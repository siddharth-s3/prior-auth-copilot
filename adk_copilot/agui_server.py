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
from fastapi.middleware.cors import CORSMiddleware
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from agents.preauth.agent import root_agent  # also sets up OTEL -> Langfuse

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
    return await _call("preauth_list_cases", {})


@app.get("/ui/cases/{case_id}")
async def case(case_id: str):
    out = await _call("preauth_case_details", {"case_id": case_id})
    if isinstance(out, dict) and out.get("isError"):
        raise HTTPException(404, out.get("raw", "not found"))
    return out


@app.get("/health")
def health():
    return {"status": "ok"}
