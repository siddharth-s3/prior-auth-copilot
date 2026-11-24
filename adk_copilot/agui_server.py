"""AG-UI + A2UI endpoint for the preauth copilot (consumed by the CopilotKit frontend).

- POST /agui          AG-UI protocol (SSE) over the same root_agent used by adk web / A2A
                      (prompts, memory, Langfuse tracing and the LiteLLM gateway all apply).
                      A2UI: the CopilotKit runtime asks for `injectA2UITool`; ag-ui-adk then injects
                      `generate_a2ui` so the agent can render A2UI surfaces in chat.
- GET  /ui/cases      case list for the UI panel   (data plane: reads the sqlite MCP server directly)
- GET  /ui/cases/{id} full case bundle for the detail panel
Adjudication is NOT done here: the UI's Adjudicate button sends "Review case <id>" through /agui so the
real supervisor workflow (specialists, memory, tracing) runs.
"""
import json
import os

from ag_ui_adk import ADKAgent, add_adk_fastapi_endpoint
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from agents.preauth.agent import root_agent  # also sets up OTEL -> Langfuse
from agents.preauth.review_client import ReviewError, call_review
from agents.preauth.a2ui_actions import apply_a2ui_action
from agents.preauth.tracing_ctx import TraceContextMiddleware

SQLITE_MCP = os.environ.get("SQLITE_MCP_URL", "http://host.docker.internal:8101/mcp")
HEADERS = {"Authorization": f"Bearer {os.environ.get('MCP_API_TOKEN', '')}"}

from google.adk.agents.run_config import StreamingMode


def _run_config(input):
    """Non-streaming LLM calls. Gemini reports "503 high demand" as an error chunk INSIDE an already-open
    stream; LiteLLM then re-raises it instead of walking the fallback chain, so the review died halfway
    (UI showed only an intermediate JSON, no final answer). Non-streaming requests fall back correctly.
    It also lets the model-tagging callback see the real model name for Langfuse."""
    cfg = agent._default_run_config(input)
    cfg.streaming_mode = StreamingMode.NONE
    return cfg


agent = ADKAgent(
    adk_agent=root_agent,
    app_name="preauth",
    user_id="reviewer",                 # single demo reviewer -> long-term memory persists across chats
    use_in_memory_services=True,
    a2ui={"inject_a2ui_tool": True},
    run_config_factory=_run_config,
)

app = FastAPI(title="preauth AG-UI")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
add_adk_fastapi_endpoint(app, agent, path="/agui")

class A2UIActionMiddleware:
    """Pure-ASGI middleware: turn an A2UI form submit into stored reviewer data + a "review again" message
    before the request reaches the agent (see agents/preauth/a2ui_actions.py)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] != "/agui":
            return await self.app(scope, receive, send)
        chunks, more = [], True
        while more:
            m = await receive()
            chunks.append(m.get("body", b""))
            more = m.get("more_body", False)
        body = b"".join(chunks)
        try:
            payload = json.loads(body)
            if ((payload.get("forwardedProps") or {}).get("a2uiAction")):
                print("[a2ui-action] form submitted by the reviewer", flush=True)

            async def submit(case_id, item, value):
                return await call_review("preauth_submit_supplement", {"case_id": case_id, "item": item, "value": value})

            new = await apply_a2ui_action(payload, submit)
            if new is not payload:
                body = json.dumps(new).encode()
        except Exception as e:   # never block a normal run because of this hook
            print(f"[a2ui-action] passthrough: {type(e).__name__}: {e}")
        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        headers = [(k, v) for k, v in scope["headers"] if k != b"content-length"]
        headers.append((b"content-length", str(len(body)).encode()))
        await self.app(dict(scope, headers=headers), replay, send)


app.add_middleware(A2UIActionMiddleware)
app.add_middleware(TraceContextMiddleware)   # evaluation runner -> real agent trace is the experiment item



async def _call(tool: str, args: dict):
    async with streamablehttp_client(SQLITE_MCP, headers=HEADERS) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(tool, args)
            text = "".join(c.text for c in res.content if getattr(c, "text", None))
            try:
                return json.loads(text)
            except Exception:
                return {"raw": text, "isError": res.isError}


@app.get("/ui/cases")
async def cases():
    rows = await _call("preauth_list_cases", {})
    try:   # overlay the simulated case state written by human review actions
        states = await call_review("preauth_list_review_states", {})
        for r in rows:
            r["review_status"] = states.get(r["case_id"])
            if r["review_status"]:
                r["status"] = r["review_status"]
    except ReviewError:
        pass   # review service down: show the base status, UI shows reviewer actions as unavailable
    return rows


@app.get("/ui/cases/{case_id}")
async def case(case_id: str):
    out = await _call("preauth_case_details", {"case_id": case_id})
    if isinstance(out, dict) and out.get("isError"):
        raise HTTPException(404, out.get("raw", "not found"))
    return out


# ---- human review (reviewer actions are REST calls from the UI -> review MCP; never the model)
class Disposition(BaseModel):
    action: str                      # ACCEPT | OVERRIDE | ESCALATE
    final_recommendation: str
    ai_recommendation: str = ""
    reason: str = ""
    reviewer: str = "reviewer"

class Clarification(BaseModel):
    question: str
    reviewer: str = "reviewer"

class Supplement(BaseModel):
    item: str
    value: str
    reviewer: str = "reviewer"


async def _review(tool, args):
    try:
        return await call_review(tool, args)
    except ReviewError as e:
        raise HTTPException(400 if "unavailable" not in str(e) else 503, str(e))


@app.post("/ui/cases/{case_id}/disposition")
async def disposition(case_id: str, body: Disposition):
    return await _review("preauth_record_disposition", {"case_id": case_id, **body.model_dump()})


@app.post("/ui/cases/{case_id}/clarification")
async def clarification(case_id: str, body: Clarification):
    return await _review("preauth_request_clarification", {"case_id": case_id, **body.model_dump()})


@app.post("/ui/cases/{case_id}/supplement")
async def supplement(case_id: str, body: Supplement):
    return await _review("preauth_submit_supplement", {"case_id": case_id, **body.model_dump()})


@app.get("/ui/cases/{case_id}/review")
async def review_state(case_id: str):
    return await _review("preauth_get_review_state", {"case_id": case_id})


@app.get("/ui/cases/{case_id}/audit")
async def audit(case_id: str):
    return await _review("preauth_list_audit", {"case_id": case_id, "limit": 100})


@app.get("/health")
def health():
    return {"status": "ok"}
