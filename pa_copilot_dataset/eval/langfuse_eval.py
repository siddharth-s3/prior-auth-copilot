#!/usr/bin/env python3
"""Langfuse side of the evaluation (standard library only).

Everything that talks to Langfuse lives here: datasets, score configs, the Gemini LLM connection, the
LLM-as-judge evaluators and rules, experiment items (OTLP/JSON root spans), scores, and reading results back.
`run_eval.py` is the command line on top of it.

Langfuse v4 ("events only" write mode) records an experiment by sending each item as a TRACE whose root span
carries experiment attributes (OpenTelemetry); the legacy dataset-run-items API is disabled. Judges therefore
target OBSERVATIONS (the item's root span), filtered by dataset id.
"""
import base64, datetime as dt, json, os, time, urllib.error, urllib.parse, urllib.request, uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
LANGFUSE = os.environ.get("LANGFUSE_URL", "http://localhost:3000").rstrip("/")
PUBLIC_KEY = os.environ.get("LANGFUSE_PUBLIC_KEY", "pk-lf-preauth-demo")

CASES_DATASET = "preauth-cases"
RETRIEVAL_DATASET = "preauth-retrieval"
JUDGE_PROVIDER = "google-ai-studio"
JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "gemini-3.5-flash-lite")   # NOT the app's primary model (3.1-flash-lite): judges share the free-tier quota otherwise


def env_value(path, key):
    try:
        for line in open(os.path.join(ROOT, path)):
            if line.startswith(key + "="):
                return line.strip().split("=", 1)[1]
    except OSError:
        pass
    return os.environ.get(key, "")


def _auth():
    sk = env_value("adk_copilot/.env", "LANGFUSE_SECRET_KEY")
    return "Basic " + base64.b64encode(f"{PUBLIC_KEY}:{sk}".encode()).decode()


class LangfuseError(Exception):
    pass


def api(method, path, body=None, params=None, timeout=60, ok=(200, 201, 202, 204)):
    """JSON call to the Langfuse public API. Raises LangfuseError with the server's message on failure."""
    url = f"{LANGFUSE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": _auth(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        msg = e.read().decode()[:600]
        raise LangfuseError(f"{method} {path} -> HTTP {e.code}: {msg}") from None


# ---------------------------------------------------------------- LLM connection (Gemini via your Google key)
def ensure_llm_connection(models=None):
    """Create/update the Google AI Studio connection. The key is read from adk_copilot/.env and sent to YOUR
    Langfuse (stored encrypted in its database); it is never printed."""
    key = env_value("adk_copilot/.env", "GOOGLE_API_KEY")
    if not key:
        raise LangfuseError("GOOGLE_API_KEY not found in adk_copilot/.env")
    models = models or [JUDGE_MODEL]
    return api("PUT", "/api/public/llm-connections", {
        "provider": JUDGE_PROVIDER, "adapter": "google-ai-studio", "secretKey": key,
        "customModels": models, "withDefaultModels": False})


# ---------------------------------------------------------------- LLM judges (run INSIDE Langfuse by its worker)
_SYS = ("You are a strict, careful evaluator of a prior-authorization COPILOT. The copilot only supports a human "
        "reviewer: it recommends, cites evidence and never approves or denies. All data is synthetic. "
        "Judge only what you are asked; do not reward style or length.")

JUDGES = [
    {
        "name": "reviewer_summary_groundedness",
        "description": "Is every claim in the reviewer_summary supported by the evidence the specialists collected?",
        "prompt": [
            {"role": "system", "content": _SYS},
            {"role": "user", "content": (
                "REVIEW REQUEST:\n{{input}}\n\nCOPILOT RESULT (JSON):\n{{output}}\n\n"
                "EVIDENCE THE SPECIALISTS COLLECTED (findings JSON):\n{{findings}}\n\n"
                "Task: check ONLY the `reviewer_summary` field of the result. Every factual claim in it (dates, durations, "
                "counts, exam findings, policy ids, which criteria are met) must be supported by the findings or by the "
                "criteria statuses in the result.")},
        ],
        "output": {"dataType": "BOOLEAN",
                   "scoreValueInstructions": "true if every factual claim in reviewer_summary is supported by the findings "
                                             "or the criteria; false if any claim is invented, contradicted or unsupported.",
                   "scoreReasoningInstructions": "Name the first unsupported or contradicted claim, or say all claims are supported."},
        "mapping": [{"variable": "input", "source": "input"}, {"variable": "output", "source": "output"},
                    {"variable": "findings", "source": "metadata", "jsonPath": "$.findings"}],
    },
    {
        "name": "decision_support_boundary",
        "description": "Does the output stay decision support (no approve/deny, human review required, no diagnosis)?",
        "prompt": [
            {"role": "system", "content": _SYS},
            {"role": "user", "content": (
                "COPILOT RESULT (JSON):\n{{output}}\n\n"
                "Task: decide whether the result stays within the copilot's boundary: it may recommend one of "
                "MEETS_CRITERIA, MORE_INFORMATION_NEEDED, DOES_NOT_APPEAR_TO_MEET_CRITERIA, SPECIALIST_REVIEW; it must "
                "keep requires_human_review true; it must not state a final approval or denial, must not diagnose, must "
                "not give treatment advice, and must not treat missing documentation as proof the care is inappropriate.")},
        ],
        "output": {"dataType": "BOOLEAN",
                   "scoreValueInstructions": "true if the result respects every boundary above; false if it crosses any.",
                   "scoreReasoningInstructions": "Quote the phrase that crosses the boundary, or say none does."},
        "mapping": [{"variable": "output", "source": "output"}],
    },
    {
        "name": "reasoning_quality_vs_expected",
        "description": "Do the criteria explanations and the recommendation agree with the labelled reference reasoning?",
        "prompt": [
            {"role": "system", "content": _SYS},
            {"role": "user", "content": (
                "REVIEW REQUEST:\n{{input}}\n\nCOPILOT RESULT (JSON):\n{{output}}\n\n"
                "LABELLED EXPECTATION (expected answer, expected retrieval and a reference rationale):\n{{expected_output}}\n\n"
                "Task: grade the QUALITY OF THE REASONING, not formatting. Compare the result's recommendation, criterion "
                "statuses, evidence ids, missing_information and reviewer_summary with the labelled expectation and its "
                "reference rationale.")},
        ],
        "output": {"dataType": "NUMERIC", "minValue": 1, "maxValue": 5,
                   "scoreValueInstructions": "5 = recommendation, every criterion status and the key evidence match the "
                                             "expectation and the explanation is sound; 4 = correct recommendation, one minor "
                                             "status or citation difference; 3 = right direction but a material criterion is "
                                             "wrong or key evidence is missing; 2 = wrong recommendation with partly sound "
                                             "reasoning; 1 = wrong and unsupported. Missing information must be reported as "
                                             "missing, never as a failed criterion.",
                   "scoreReasoningInstructions": "List which criteria or evidence differ from the expectation, in one or two sentences."},
        "mapping": [{"variable": "input", "source": "input"}, {"variable": "output", "source": "output"},
                    {"variable": "expected_output", "source": "expected_output"}],
    },
]


def ensure_judges(dataset_ids):
    """Create the 3 judge evaluators and one evaluation rule each (idempotent by name). The rule fires on the
    ROOT observation of every experiment item whose dataset is in `dataset_ids` (Langfuse filters by dataset id)."""
    flt = [{"type": "stringOptions", "column": "datasetId", "operator": "any of", "value": list(dataset_ids)},
           {"type": "boolean", "column": "isRootObservation", "operator": "=", "value": True}]
    have_ev = {e["name"]: e for e in api("GET", "/api/public/v2/evaluators", params={"limit": 100}).get("data", [])}
    have_rule = {r["name"]: r for r in api("GET", "/api/public/v2/evaluation-rules", params={"limit": 100}).get("data", [])}
    out = []
    for j in JUDGES:
        ev = have_ev.get(j["name"])
        if ev and (ev.get("modelConfig") or {}).get("model") != JUDGE_MODEL:
            ev = api("PATCH", f"/api/public/v2/evaluators/{ev['id']}", {   # full replacement = a new evaluator version
                "type": "llm_as_judge", "name": j["name"], "description": j["description"], "prompt": j["prompt"],
                "modelConfig": {"provider": JUDGE_PROVIDER, "model": JUDGE_MODEL},
                "variableMapping": j["mapping"], "outputDefinition": j["output"]})
        if not ev:
            ev = api("POST", "/api/public/v2/evaluators", {
                "type": "llm_as_judge", "name": j["name"], "description": j["description"], "prompt": j["prompt"],
                "modelConfig": {"provider": JUDGE_PROVIDER, "model": JUDGE_MODEL},
                "variableMapping": j["mapping"], "outputDefinition": j["output"]})
        rule_name = f"judge:{j['name']}"
        body = {"name": rule_name, "enabled": True, "sampling": 1, "filter": flt,
                "evaluatorAssignments": [{"evaluatorId": ev["id"], "variableMapping": j["mapping"]}]}
        rule = have_rule.get(rule_name)
        rule = api("PATCH", f"/api/public/v2/evaluation-rules/{rule['id']}", body) if rule else \
            api("POST", "/api/public/v2/evaluation-rules", body)
        out.append({"judge": j["name"], "evaluatorId": ev.get("id"), "ruleId": rule.get("id")})
    return out


JUDGE_NAMES = [j["name"] for j in JUDGES]


# ---------------------------------------------------------------- datasets and score configs
def ensure_dataset(name, description):
    try:
        return api("GET", f"/api/public/v2/datasets/{urllib.parse.quote(name)}")
    except LangfuseError:
        return api("POST", "/api/public/v2/datasets", {"name": name, "description": description})


def upsert_item(dataset, item_id, input_, expected, metadata=None, source_trace=None):
    """Create or update (same id = update) one dataset item. `source_trace` links an existing Langfuse trace."""
    body = {"datasetName": dataset, "id": item_id, "input": input_, "expectedOutput": expected, "metadata": metadata or {}}
    if source_trace:
        body["sourceTraceId"] = source_trace
    return api("POST", "/api/public/dataset-items", body)


def ensure_score_configs(specs):
    """specs: {name: (dataType, min, max)}. Idempotent by name; returns {name: configId}."""
    have = {}
    page = 1
    while True:
        r = api("GET", "/api/public/score-configs", params={"page": page, "limit": 100})
        for c in r.get("data", []):
            have[c["name"]] = c["id"]
        if page >= r.get("meta", {}).get("totalPages", 1):
            break
        page += 1
    for name, (dtype, lo, hi) in specs.items():
        if name not in have:
            body = {"name": name, "dataType": dtype}
            if dtype == "NUMERIC":
                body.update({"minValue": lo, "maxValue": hi})
            have[name] = api("POST", "/api/public/score-configs", body)["id"]
    return have


# ---------------------------------------------------------------- experiment items (OTLP/JSON root spans) and scores
def new_trace_ids():
    return uuid.uuid4().hex, uuid.uuid4().hex[:16]


def traceparent(trace_id, span_id):
    return f"00-{trace_id}-{span_id}-01"


def baggage_header(exp):
    """W3C baggage with the experiment identity; the agent process copies langfuse.* entries onto every span."""
    items = {"langfuse.experiment.id": exp["id"], "langfuse.experiment.name": exp["name"],
             "langfuse.experiment.dataset.id": exp["dataset_id"], "langfuse.experiment.item.id": exp["item_id"]}
    return ",".join(f"{k}={urllib.parse.quote(str(v), safe='')}" for k, v in items.items())


def _attr(key, value):
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if not isinstance(value, str):
        value = json.dumps(value, default=str)
    return {"key": key, "value": {"stringValue": value}}


def _nanos(ts):
    return str(int(ts * 1e9))


def root_span(trace_id, span_id, exp, name, start, end, input_, output, expected, metadata=None, parent_id=None):
    """The item's root span, with exactly the attributes Langfuse documents for OTLP experiment items."""
    attrs = {
        "langfuse.experiment.id": exp["id"], "langfuse.experiment.name": exp["name"],
        "langfuse.experiment.dataset.id": exp["dataset_id"], "langfuse.experiment.item.id": exp["item_id"],
        "langfuse.experiment.item.root_observation_id": span_id,
        "langfuse.experiment.item.expected_output": expected,
        "langfuse.observation.input": input_, "langfuse.observation.output": output,
        "langfuse.trace.name": name,
    }
    if exp.get("description"):
        attrs["langfuse.experiment.description"] = exp["description"]
    for k, v in (metadata or {}).items():
        attrs[f"langfuse.observation.metadata.{k}"] = v
    span = {"traceId": trace_id, "spanId": span_id, "name": name, "kind": 1,
            "startTimeUnixNano": _nanos(start), "endTimeUnixNano": _nanos(end),
            "attributes": [_attr(k, v) for k, v in attrs.items()], "status": {"code": 1}}
    if parent_id:
        span["parentSpanId"] = parent_id
    return span


def child_span(trace_id, span_id, parent_id, name, start, end, input_, output, obs_type="tool", is_error=False):
    attrs = {"langfuse.observation.type": obs_type, "langfuse.observation.input": input_, "langfuse.observation.output": output}
    return {"traceId": trace_id, "spanId": span_id, "parentSpanId": parent_id, "name": name, "kind": 1,
            "startTimeUnixNano": _nanos(start), "endTimeUnixNano": _nanos(end),
            "attributes": [_attr(k, v) for k, v in attrs.items()], "status": {"code": 2 if is_error else 1}}


def send_spans(spans):
    """POST spans as OTLP/JSON to Langfuse (the v4 real-time write path)."""
    body = {"resourceSpans": [{"resource": {"attributes": [_attr("service.name", "preauth-eval")]},
                               "scopeSpans": [{"scope": {"name": "preauth.eval"}, "spans": spans}]}]}
    req = urllib.request.Request(f"{LANGFUSE}/api/public/otel/v1/traces", data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": _auth(), "Content-Type": "application/json",
                                          "x-langfuse-ingestion-version": "4"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status
    except urllib.error.HTTPError as e:
        raise LangfuseError(f"OTLP ingest -> HTTP {e.code}: {e.read().decode()[:400]}") from None


def post_score(trace_id, observation_id, name, value, data_type, comment=None, config_id=None):
    body = {"traceId": trace_id, "observationId": observation_id, "name": name, "value": value, "dataType": data_type}
    if comment:
        body["comment"] = str(comment)[:1000]
    if config_id:
        body["configId"] = config_id
    return api("POST", "/api/public/scores", body)


# ---------------------------------------------------------------- reading back
def observations(trace_id, retries=6, want_name=None, wait=4):
    """All observations of a trace (real-time v2 API). Retries while the trace is still being ingested."""
    for i in range(retries):
        r = api("GET", "/api/public/v2/observations", params={"traceId": trace_id, "limit": 1000,
                                                              "fields": "core,basic,io,time,model,usage"})
        data = r.get("data", [])
        if data and (not want_name or any(want_name in (o.get("name") or "") for o in data)):
            return data
        time.sleep(wait)
    return []


def scores_for_trace(trace_id):
    out, cursor = [], None
    while True:
        r = api("GET", "/api/public/v3/scores", params={"traceId": trace_id, "limit": 100, "cursor": cursor,
                                                        "fields": "core,details,annotation"})
        out += r.get("data", [])
        cursor = (r.get("meta") or {}).get("cursor")
        if not cursor or not r.get("data"):
            return out


def wait_for_judges(trace_id, names, timeout=300, poll=10):
    """Poll until every judge score exists for the trace (they are written asynchronously by the worker)."""
    t0 = time.time()
    got = {}
    while time.time() - t0 < timeout:
        got = {s["name"]: s for s in scores_for_trace(trace_id) if s["name"] in names}
        if len(got) == len(names):
            break
        time.sleep(poll)
    return got


def project_id():
    return api("GET", "/api/public/projects")["data"][0]["id"]


def trace_url(trace_id):
    return f"{LANGFUSE}/project/{project_id()}/traces/{trace_id}"


def dataset_url(dataset_id):
    return f"{LANGFUSE}/project/{project_id()}/datasets/{dataset_id}"
