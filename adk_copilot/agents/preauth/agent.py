"""Prior-auth copilot: supervisor + 4 specialist sub-agents (Google ADK).

LLM calls go through the LiteLLM proxy (gateway/) — model group "preauth-model" with
simple-shuffle between Gemini flash-lite and Groq gpt-oss-20b, 30s timeout fallback.
Set DIRECT_GEMINI=1 to bypass the proxy (direct Gemini with HttpRetryOptions).

Tool plane: both MCP servers sit behind the LiteLLM MCP gateway (single URL, tool names are
namespaced, e.g. "preauth_sqlite-preauth_case_details"). The gateway injects the upstream
Bearer token; gateway-side auth is off by request (lab posture).
"""
import os

from google.adk.agents.llm_agent import LlmAgent
from google.adk.agents.sequential_agent import SequentialAgent
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams

from . import prompts
from .memory import recall_memories, save_memory

# ---- tracing: ADK events -> OTEL -> Langfuse (and Lens if LENS_URL/LENS_TRACING_KEY set)
# NOTE: prompts are fetched via Langfuse's plain REST API (no langfuse SDK) so the SDK cannot
# hijack the global OTel tracer provider, which must stay ours for span export to work.
def _setup_tracing():
    pk, sk = os.environ.get("LANGFUSE_PUBLIC_KEY"), os.environ.get("LANGFUSE_SECRET_KEY")
    if not (pk and sk) or os.environ.get("DISABLE_TRACING"):
        return
    try:
        import base64
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from openinference.instrumentation.google_adk import GoogleADKInstrumentor

        # `adk web` installs its own global TracerProvider (for its debug UI) before this module
        # loads, and OTel refuses to override it. Reuse it when it's an SDK provider so our
        # exporters attach to the provider ADK actually emits spans on.
        existing = trace.get_tracer_provider()
        provider = existing if hasattr(existing, "add_span_processor") else TracerProvider()
        lf = os.environ.get("LANGFUSE_HOST", "http://host.docker.internal:3000")
        basic = base64.b64encode(f"{pk}:{sk}".encode()).decode()
        from .tracing_ctx import ExperimentAttributes
        provider.add_span_processor(ExperimentAttributes())   # evaluation runs: link spans to the Langfuse experiment
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
            endpoint=f"{lf.rstrip('/')}/api/public/otel/v1/traces",
            # Langfuse v4 requires this header from direct OTLP exporters
            headers={"Authorization": f"Basic {basic}", "x-langfuse-ingestion-version": "4"})))
        lens_url, lens_key = os.environ.get("LENS_URL"), os.environ.get("LENS_TRACING_KEY")
        if lens_url and lens_key:
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
                endpoint=f"{lens_url.rstrip('/')}/v1/traces",
                headers={"Authorization": f"Bearer {lens_key}"})))
        if provider is not existing:
            trace.set_tracer_provider(provider)
        GoogleADKInstrumentor().instrument()
        print(f"[tracing] OTEL -> Langfuse ({lf})" + (" + Lens" if lens_url else ""))
    except Exception as e:
        print(f"[tracing] disabled: {e}")

_setup_tracing()

def _load_langfuse_prompts():
    pk, sk = os.environ.get("LANGFUSE_PUBLIC_KEY"), os.environ.get("LANGFUSE_SECRET_KEY")
    if os.environ.get("SKIP_LANGFUSE_PROMPTS"):   # seeding: importing this module must not overwrite local prompts
        return
    if not (pk and sk):
        print("[prompts] no Langfuse keys — using local prompts.py"); return
    import requests as _rq
    host = os.environ.get("LANGFUSE_HOST", "http://host.docker.internal:3000")
    ok = True
    for slug, const in {"supervisor": "SUPERVISOR", "clinical_evidence": "CLINICAL_EVIDENCE",
                        "policy_rag": "POLICY_RAG", "coverage_eligibility": "COVERAGE_ELIGIBILITY",
                        "criteria_quality": "CRITERIA_QUALITY", "memory": "MEMORY"}.items():
        try:
            r = _rq.get(f"{host.rstrip('/')}/api/public/v2/prompts/preauth%2F{slug}",
                        params={"label": "production"}, auth=(pk, sk), timeout=10)
            r.raise_for_status()
            setattr(prompts, const, r.json()["prompt"])
        except Exception as e:
            ok = False; print(f"[prompts] Langfuse miss for preauth/{slug}, using local: {e}")
    print("[prompts] Langfuse prompt management active" if ok else "[prompts] partial/local prompts in use")

_load_langfuse_prompts()

# ---- model: LiteLLM proxy by default, direct Gemini as escape hatch
DIRECT = os.environ.get("DIRECT_GEMINI") == "1"
if DIRECT:
    from google.adk.models.google_llm import Gemini
    from google.genai import types as genai_types

    def make_model():
        return Gemini(
            model=os.environ.get("ADK_MODEL", "gemini-3.1-flash-lite"),
            retry_options=genai_types.HttpRetryOptions(
                attempts=10, initial_delay=4.0, exp_base=2.0, jitter=0.5,
                http_status_codes=[429, 500, 502, 503, 504]),
        )
else:
    from google.adk.models.lite_llm import LiteLlm

    def make_model():
        # openai/* -> LiteLLM picks up OPENAI_API_BASE/OPENAI_API_KEY (proxy is unauthenticated)
        # explicit client-side timeout: litellm's default is ~100 minutes, so one stalled connection could freeze a
        # whole review. The gateway still walks its fallback chain; this only bounds how long we wait for it.
        return LiteLlm(model=os.environ.get("AGENT_LLM_MODEL", "openai/preauth-model"),
                       timeout=float(os.environ.get("LLM_TIMEOUT_S", "150")))

MODEL = make_model()

MCP_GATEWAY = os.environ.get("MCP_GATEWAY_URL", "http://host.docker.internal:4000/mcp")
LITELLM_KEY = os.environ.get("LITELLM_MASTER_KEY", "")

def _gw_headers(ctx):
    # LiteLLM's own key goes on x-litellm-api-key so Authorization stays free for upstreams
    return {"x-litellm-api-key": LITELLM_KEY}

def toolset(only):
    # bounded waits: 15 s to connect, 90 s for a tool result (a stuck MCP server fails fast instead of hanging)
    return McpToolset(connection_params=StreamableHTTPConnectionParams(
                          url=MCP_GATEWAY, timeout=float(os.environ.get("MCP_CONNECT_TIMEOUT_S", "15")),
                          sse_read_timeout=float(os.environ.get("MCP_READ_TIMEOUT_S", "90"))),
                      header_provider=_gw_headers, tool_filter=only)

def _tag_model(callback_context, llm_response):
    """Stamp the real model (as reported by the LiteLLM gateway, e.g. gemini-3.1-flash-lite or
    openai/gpt-oss-20b) onto the current call_llm span so Langfuse shows it instead of the
    "preauth-model" alias. Langfuse reads gen_ai.request.model / gen_ai.response.model."""
    try:
        real = getattr(llm_response, "model_version", None)
        if real:
            from opentelemetry import trace
            span = trace.get_current_span()
            if span.is_recording():
                span.set_attribute("gen_ai.request.model", real)
                span.set_attribute("gen_ai.response.model", real)
                span.set_attribute("llm.model_name", real)
                # LiteLLM semantic-cache hits arrive as "<model> (cache hit)" (gateway hook).
                hit = "(cache hit)" in real
                span.set_attribute("langfuse.observation.metadata.cache_hit", hit)
                if hit:
                    span.set_attribute("langfuse.observation.metadata.cache_type", "litellm-qdrant-semantic")
    except Exception:
        pass
    return None

# gateway-side namespaced tool names
T_CASES = "preauth_sqlite-preauth_list_cases"
T_DETAILS = "preauth_sqlite-preauth_case_details"
T_POLICY_CTX = "preauth_sqlite-preauth_policy_context"
T_POLICIES = "preauth_qdrant-preauth_policies"
T_NOTES = "preauth_qdrant-preauth_notes"

clinical_evidence_agent = LlmAgent(
    name="clinical_evidence_agent",
    model=MODEL,
    after_model_callback=_tag_model,
    description="Owns the case record. Lists cases and fetches/renders case details on request "
                "(fetch intents: 'show my cases', 'show PA-004'); in a review, collects diagnoses, "
                "encounters, observations and note excerpts and separates cited facts from "
                "ambiguities and gaps.",
    instruction=prompts.CLINICAL_EVIDENCE,
    tools=[toolset([T_CASES, T_DETAILS]), toolset([T_NOTES])],
    output_key="clinical_evidence",
)




memory_agent = LlmAgent(
    name="memory_agent",
    model=MODEL,
    after_model_callback=_tag_model,
    description="Long-term memory (mem0). Recalls the reviewer's preferences, past case outcomes and "
                "overrides before a review; saves preferences, overrides and case outcomes. The only "
                "agent with memory tools.",
    instruction=prompts.MEMORY,
    tools=[recall_memories, save_memory],
)

# ---------------------------------------------------------------------------------------------
# REVIEW PIPELINE. Only "adjudicate / review case X" uses it, through the run_review tool below.
# Fetch ("show PA-001", "my cases") and memory intents are untouched: the supervisor still transfers
# straight to the one specialist that owns them. Why a pipeline for reviews: when the 4 specialists were
# chained by the LLM ("output JSON then transfer back"), weaker/fallback models often just stopped after
# their JSON and the final answer was never produced. Here the ORDER is code; the model only does the work
# inside each step. These are separate agent instances (an ADK agent can have only one parent).
# ---------------------------------------------------------------------------------------------
import asyncio, json, re, uuid
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as _gtypes
from .review_client import ReviewError, call_review, call_sqlite
from .validate import apply_checks, parse_json as _parse_json
from .a2ui_catalog import CatalogError, review_surfaces

PIPELINE_SUFFIX = """

PIPELINE MODE (automated review): you are one step of a fixed review pipeline and there is no transfer
tool. Work in your REVIEW mode for the case named in the request. Reply with ONLY your JSON object (a
```json block is fine). No 'Next steps' line, no extra prose."""
CRITERIA_EXTRA = """
Also include "reviewer_summary" in your JSON: 2-4 plain sentences for the human reviewer (the
recommendation, the deciding criteria, what is missing). Use only facts from the earlier steps' JSON."""


def _step(name, desc, instruction, tools, out_key, extra=""):
    return LlmAgent(name=name, model=MODEL, after_model_callback=_tag_model, description=desc,
                    instruction=instruction + extra + PIPELINE_SUFFIX, tools=tools, output_key=out_key,
                    disallow_transfer_to_parent=True, disallow_transfer_to_peers=True)


review_pipeline = SequentialAgent(
    name="review_pipeline",
    description="Fixed-order case review: clinical evidence -> policy -> coverage -> criteria/quality.",
    sub_agents=[
        _step("clinical_evidence_agent", "collects the case record", prompts.CLINICAL_EVIDENCE,
              [toolset([T_CASES, T_DETAILS]), toolset([T_NOTES])], "clinical_evidence"),
        _step("policy_rag_agent", "finds the governing policy", prompts.POLICY_RAG,
              [toolset([T_POLICY_CTX]), toolset([T_POLICIES])], "policy_findings"),
        _step("coverage_eligibility_agent", "coverage + authorization history", prompts.COVERAGE_ELIGIBILITY,
              [toolset([T_DETAILS, T_CASES])], "coverage_check"),
        _step("criteria_quality_agent", "maps evidence to criteria, drafts the result", prompts.CRITERIA_QUALITY,
              [toolset([T_POLICY_CTX, T_DETAILS])], "criteria_matrix", extra=CRITERIA_EXTRA),
    ],
)

RECOMMENDATIONS = ("MEETS_CRITERIA", "MORE_INFORMATION_NEEDED", "DOES_NOT_APPEAR_TO_MEET_CRITERIA",
                   "SPECIALIST_REVIEW")


async def run_review(case_id: str, tool_context) -> dict:
    """Run the COMPLETE prior-authorization review for one case (clinical evidence, policy, coverage,
    criteria check) and return the validated result object. Call exactly once per review request, with the
    case id the user gave (e.g. "PA-001"). Also publishes the result to the app's shared state.

    Args:
        case_id: the case to review, e.g. "PA-001".
    """
    memory = await asyncio.to_thread(recall_memories, "reviewer formatting preferences and standing overrides",
                                     tool_context)
    # reviewer-supplied data (REV-NNN) from earlier human review actions: part of the next review's input,
    # clearly labelled as reviewer assertions (never as the original clinical record)
    supplements = []
    try:
        supplements = (await call_review("preauth_get_review_state", {"case_id": case_id})).get("supplements", [])
    except ReviewError:
        pass   # review service down: review proceeds from the record alone
    supp_text = ""
    if supplements:
        supp_text = ("\n\nREVIEWER-SUPPLIED DATA (assertions by the human reviewer, not part of the original "
                     "record; cite as REV-NNN and say so in your findings):\n" +
                     "\n".join(f"- {s['supp_id']}: {s['item']} = {s['value']}" for s in supplements))
    last_err = None
    for attempt in (1, 2):
        svc = InMemorySessionService()
        sid = f"review-{uuid.uuid4().hex[:8]}"
        await svc.create_session(app_name="preauth_review", user_id=tool_context.user_id, session_id=sid)
        runner = Runner(agent=review_pipeline, app_name="preauth_review", session_service=svc)
        msg = _gtypes.Content(role="user", parts=[_gtypes.Part(text=(
            f"REVIEW mode: review case {case_id}. Produce your findings JSON for this case." + supp_text))])
        steps = []
        try:
            async for ev in runner.run_async(user_id=tool_context.user_id, session_id=sid, new_message=msg):
                if ev.is_final_response() and ev.author:
                    steps.append(ev.author)
            st = (await svc.get_session(app_name="preauth_review", user_id=tool_context.user_id,
                                        session_id=sid)).state
        except Exception as e:  # LLM/tool failure inside the pipeline: retry once, then report honestly
            last_err = f"{type(e).__name__}: {e}"
            continue
        result = _parse_json(st.get("criteria_matrix"))
        if result:
            break
        last_err = "criteria_quality_agent did not return a parseable result object"
    else:
        return {"error": f"review pipeline failed for {case_id}: {last_err}. Do not fabricate a result."}

    findings = {k: _parse_json(st.get(k)) or st.get(k) for k in
                ("clinical_evidence", "policy_findings", "coverage_check")}
    # deterministic checks (no LLM): schema, citations vs the real record, recommendation precedence
    try:
        bundle = await call_sqlite("preauth_case_details", {"case_id": case_id})
    except ReviewError:
        bundle = None
    result = apply_checks(result, bundle, findings.get("policy_findings"), supplements)
    result.setdefault("case_id", case_id)
    # AG-UI shared state: the UI panel reads this (STATE_DELTA), no scraping of chat text.
    tool_context.state["adjudication"] = {"case_id": result["case_id"], "result": result,
                                          "findings": findings, "steps": steps, "supplements_used": supplements,
                                          "reviewer_memory": memory.get("memories", [])}
    # generate_a2ui's sub-model only sees user/model TEXT turns (never tool results), so on its own it
    # invents a plausible-looking card. AG-UI "context" entries ARE put in its prompt: hand it the real result.
    ctx_desc = "AUTHORITATIVE prior-authorization review result: render exactly these values, invent nothing"
    ctx = [c for c in (tool_context.state.get("_ag_ui_context") or [])
           if not (isinstance(c, dict) and c.get("description") == ctx_desc)]
    tool_context.state["_ag_ui_context"] = ctx + [{"description": ctx_desc, "value": json.dumps(result)}]
    return {"result": result, "reviewer_memory": memory.get("memories", []), "steps_completed": steps}


def render_review_ui(tool_context) -> dict:
    """Render the review screens for the case just reviewed: the criteria checklist and, when information
    is missing, a case-specific form for it. Built in code from the validated result (fixed A2UI catalog),
    so the values are always the real ones. Call once, right after run_review, with no arguments."""
    adj = tool_context.state.get("adjudication")
    if not adj or not adj.get("result"):
        return {"error": "no review result in this session; call run_review first"}
    try:
        return review_surfaces(adj["result"])
    except CatalogError as e:   # never render something outside the catalog
        return {"error": f"review screen rejected by the A2UI catalog check: {e}"}


root_agent = LlmAgent(
    name="supervisor",
    model=MODEL,
    after_model_callback=_tag_model,
    description="Supervisor: routes PA case reviews to specialist agents, manages shared case "
                "state, limits retries, detects disagreement, composes the final summary.",
    instruction=prompts.SUPERVISOR,
    sub_agents=[clinical_evidence_agent, memory_agent],
    tools=[run_review, render_review_ui],   # review intents -> one deterministic pipeline; fetch/memory intents -> transfer
)
