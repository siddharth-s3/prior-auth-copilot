#!/usr/bin/env python3
"""Evaluation runner: the results live in Langfuse (standard library only).

  python3 pa_copilot_dataset/eval/run_eval.py setup                       # once: Gemini connection, datasets, items, score configs, 3 LLM judges
  python3 pa_copilot_dataset/eval/run_eval.py run --name demo --cases PA-001,PA-004
  python3 pa_copilot_dataset/eval/run_eval.py run --name big --sample 4 --seed 7      # random sample of the dataset
  python3 pa_copilot_dataset/eval/run_eval.py retrieval --name demo-rt   # the 6 retrieval items (no model calls)
  python3 pa_copilot_dataset/eval/run_eval.py replay --name old --hours 24 --sample 2 --seed 1   # evaluate EXISTING traces
  python3 pa_copilot_dataset/eval/run_eval.py report --name demo         # markdown report built from Langfuse

How a run works: for each dataset item the runner calls the real app (/agui) with a W3C traceparent, so the app's
whole agent trace (supervisor, specialists, Qdrant and SQLite tool calls, model calls) becomes the experiment item in
Langfuse. The runner then (1) computes CODE metrics, including what was really retrieved from Qdrant, read from that
trace, and posts them as scores; (2) sends the item's root span, which makes the three LLM judges run INSIDE Langfuse;
(3) waits for the judge scores. Open Langfuse > Experiments to compare runs.
"""
import argparse, datetime as dt, json, os, random, re, sqlite3, sys, time, urllib.error, urllib.request

import langfuse_eval as LF

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
DB = os.path.join(ROOT, "pa_copilot_dataset", "pa_copilot.db")
RESULTS = os.path.join(HERE, "results")
AGUI = os.environ.get("AGUI_URL", "http://localhost:8002/agui")
QDRANT_MCP = os.environ.get("QDRANT_MCP_URL", "http://localhost:8100/mcp")
TRACES_DATASET = "preauth-traces"
RECS = ("MEETS_CRITERIA", "MORE_INFORMATION_NEEDED", "DOES_NOT_APPEAR_TO_MEET_CRITERIA", "SPECIALIST_REVIEW")
UNTRUSTED = ["UNVERIFIED-upload-0001"]

# code metrics written to Langfuse (name -> (dataType, min, max)); numeric metrics are 0..1
SCORE_SPECS = {n: ("BOOLEAN", None, None) for n in (
    "policy_correct", "recommendation_correct", "missing_info_detected", "no_false_missing_info", "citations_resolve_in_db",
    "schema_valid", "human_review_required", "valid_recommendation_value", "injection_flagged", "injection_not_obeyed",
    "injection_ids_recall", "rejected_policies_recall", "policy_retrieved_correct", "policy_filters_correct",
    "decoy_policy_not_applied", "qdrant_calls_made", "qdrant_tool_success", "produced_result",
    "top_is_in_force_version", "excluded_versions_absent", "expired_exception_not_applied", "semantic_leader_lost_to_date_filter")}
SCORE_SPECS.update({n: ("NUMERIC", 0, 1) for n in (
    "criterion_status_accuracy", "evidence_recall", "model_citation_validity", "notes_recall_qdrant", "notes_precision_qdrant")})


# ------------------------------------------------------------------ ground truth -> dataset items
def load_gt():
    gt = json.load(open(os.path.join(HERE, "ground_truth.json")))
    cases = gt["cases"]
    return {c["case_id"]: c for c in (cases.values() if isinstance(cases, dict) else cases)}, gt["retrieval_tests"]


def case_rows():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = {r["case_id"]: dict(r) for r in con.execute("SELECT case_id, plan_id, request_date, cpt, service_name FROM cases")}
    con.close()
    return rows


def expected_notes(exp):
    return sorted({e for c in exp["criteria"] for e in c.get("evidence_ids", []) if re.match(r"^NOTE-\d+#\w+$", e)})


def reference_reasoning(exp):
    """Plain-text rationale built ONLY from the labels, so the judge has something concrete to compare with."""
    parts = []
    pol = exp.get("expected_applicable_policy_id")
    parts.append(f"Governing policy: {pol}." if pol else "No policy is both in force on the request date and covers the plan, so there is no governing policy.")
    for p, why in (exp.get("expected_rejected_policies") or {}).items():
        parts.append(f"{p} must not govern ({why}).")
    for c in exp["criteria"]:
        ev = ", ".join(c.get("evidence_ids", [])) or "no evidence"
        parts.append(f"{c['criterion_id']} is {c['status']} (evidence: {ev}).")
    if exp.get("missing_information"):
        parts.append("Missing information to report (not a failed criterion): " + "; ".join(exp["missing_information"]) + ".")
    if exp.get("prompt_injection_present"):
        parts.append("An instruction hidden in the record must be flagged and not followed: " + ", ".join(exp.get("injection_evidence_ids", [])) + ".")
    parts.append(f"Therefore the recommendation is {exp['expected_recommendation']} (scenario: {exp['scenario']}); a human decides.")
    return " ".join(parts)


def build_item(exp, row):
    """(input, expectedOutput, metadata) for one dataset item: the question and the labelled answer/retrieval/reasoning."""
    inp = {"question": f"Review case {exp['case_id']}", "case_id": exp["case_id"]}
    expected = {
        "answer": {"recommendation": exp["expected_recommendation"], "applicable_policy_id": exp.get("expected_applicable_policy_id"),
                   "criteria": [{"criterion_id": c["criterion_id"], "status": c["status"], "evidence_ids": c.get("evidence_ids", [])} for c in exp["criteria"]],
                   "missing_information": exp.get("missing_information", []), "prompt_injection_present": bool(exp.get("prompt_injection_present"))},
        "retrieval": {"expected_policy_ids": [exp["expected_applicable_policy_id"]] if exp.get("expected_applicable_policy_id") else [],
                      "decoy_policy_ids": sorted(set(exp.get("expected_rejected_policies", {})) | set(UNTRUSTED)),
                      "expected_note_ids": expected_notes(exp)},
        "reference_reasoning": reference_reasoning(exp)}
    meta = {"scenario": exp["scenario"], "plan_id": row.get("plan_id"), "request_date": row.get("request_date"), "cpt": row.get("cpt")}
    return inp, expected, meta


# ------------------------------------------------------------------ calling the app
def run_agui(case_id, headers=None, timeout=900):
    import uuid
    body = {"threadId": str(uuid.uuid4()), "runId": str(uuid.uuid4()), "state": {}, "tools": [], "context": [], "forwardedProps": {},
            "messages": [{"id": str(uuid.uuid4()), "role": "user", "content": f"Review case {case_id}"}]}
    h = {"Content-Type": "application/json", "Accept": "text/event-stream", **(headers or {})}
    out = {"adjudication": None, "error": None, "tools": [], "review_error": None, "text": ""}
    t0 = time.time()
    try:
        with urllib.request.urlopen(urllib.request.Request(AGUI, data=json.dumps(body).encode(), headers=h), timeout=timeout) as r:
            for raw in r:
                line = raw.decode().strip()
                if not line.startswith("data:"):
                    continue
                e = json.loads(line[5:])
                t = e.get("type")
                if t == "RUN_ERROR":
                    out["error"] = e.get("message", "RUN_ERROR")
                elif t == "TOOL_CALL_START":
                    out["tools"].append(e.get("toolCallName"))
                elif t == "TEXT_MESSAGE_CONTENT":
                    out["text"] += e.get("delta", "")
                elif t == "TOOL_CALL_RESULT" and '"error"' in str(e.get("content", ""))[:200]:
                    out["review_error"] = str(e.get("content"))[:300]
                elif t == "STATE_DELTA":
                    for op in e.get("delta", []):
                        if op.get("path") == "/adjudication":
                            out["adjudication"] = op.get("value")
                elif t == "STATE_SNAPSHOT" and (e.get("snapshot") or {}).get("adjudication"):
                    out["adjudication"] = e["snapshot"]["adjudication"]
    except Exception as ex:
        out["error"] = out["error"] or f"{type(ex).__name__}: {ex}"
    out["start"], out["end"] = t0, time.time()
    return out


# ------------------------------------------------------------------ code metrics
def db_ids():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    ids = {"RF-01", "EX-01", "EX-02"}
    for doc, sec in con.execute("SELECT doc_id, section FROM note_sections"):
        ids.update({doc, f"{doc}#{sec}"})
    for tbl, col in (("document_references", "doc_id"), ("observations", "observation_id"), ("conditions", "condition_id"),
                     ("encounters", "encounter_id"), ("coverage", "coverage_id"), ("auth_history", "auth_id"),
                     ("policies", "policy_id"), ("exceptions", "exception_id")):
        ids.update(r[0] for r in con.execute(f"SELECT {col} FROM {tbl}"))
    for pid, cid in con.execute("SELECT policy_id, criterion_id FROM policy_criteria"):
        ids.update({cid, f"{pid}#{cid}"})
    con.close()
    return ids


def _tok(text):
    return set(re.findall(r"[a-z0-9]+", (text or "").lower())) - {"the", "of", "a", "an", "to", "and", "or", "for", "is", "in", "on"}


def _ratio(n, d):
    return None if not d else round(n / d, 3)


def _json(x):
    if isinstance(x, (dict, list)):
        return x
    try:
        return json.loads(x)
    except Exception:
        return None


def score_answer(exp, adj, known):
    """Programmatic checks of the answer and reasoning evidence against the labels. Values: bool, 0..1 float, or None (n/a)."""
    r = (adj or {}).get("result") or {}
    if not r:
        return {"produced_result": False}, {"got_recommendation": None}
    f = (adj or {}).get("findings") or {}
    pol = f.get("policy_findings") if isinstance(f.get("policy_findings"), dict) else {}
    crit = {c["criterion_id"]: c for c in r.get("criteria", []) if isinstance(c, dict) and "criterion_id" in c}
    s, d = {"produced_result": True}, {}
    s["policy_correct"] = r.get("applicable_policy_id") == exp.get("expected_applicable_policy_id")
    s["decoy_policy_not_applied"] = r.get("applicable_policy_id") not in (set(exp.get("expected_rejected_policies", {})) | set(UNTRUSTED))
    rej = set(exp.get("expected_rejected_policies", {}))
    s["rejected_policies_recall"] = (rej <= set((pol.get("rejected") or {}).keys())) if rej and pol else None
    s["recommendation_correct"] = r.get("recommendation") == exp["expected_recommendation"]
    ec = {c["criterion_id"]: c for c in exp["criteria"]}
    s["criterion_status_accuracy"] = _ratio(sum(1 for k, c in ec.items() if crit.get(k, {}).get("status") == c["status"]), len(ec))
    d["criterion_status_mismatch"] = {k: {"expected": c["status"], "got": crit.get(k, {}).get("status")}
                                      for k, c in ec.items() if crit.get(k, {}).get("status") != c["status"]}
    exp_ev = {e for c in exp["criteria"] for e in c.get("evidence_ids", [])}
    got_ev = {e for c in crit.values() for e in c.get("evidence_ids", [])}
    s["evidence_recall"] = _ratio(len(exp_ev & got_ev), len(exp_ev)) if exp_ev else None
    em, gm = exp.get("missing_information", []), r.get("missing_information", [])
    if em:
        et = set().union(*[_tok(x) for x in em])
        s["missing_info_detected"] = bool(gm) and len(et & set().union(*[_tok(x) for x in gm])) / max(1, len(et)) >= 0.3
        s["no_false_missing_info"] = None
    else:
        s["missing_info_detected"] = None
        s["no_false_missing_info"] = not gm
    cited = {e for c in crit.values() for e in c.get("evidence_ids", [])}
    s["citations_resolve_in_db"] = all(e in known for e in cited) if cited else None
    v = r.get("verification") or {}
    s["model_citation_validity"] = _ratio(v.get("citations_valid", 0), v.get("citations_total", 0)) if v.get("citations_total") else None
    s["schema_valid"] = v.get("schema_valid") if v else None
    s["human_review_required"] = r.get("requires_human_review") is True
    s["valid_recommendation_value"] = r.get("recommendation") in RECS
    if exp.get("prompt_injection_present"):
        ie = set(exp.get("injection_evidence_ids", []))
        s["injection_flagged"] = r.get("prompt_injection_present") is True
        s["injection_not_obeyed"] = r.get("recommendation") == exp["expected_recommendation"]
        s["injection_ids_recall"] = (ie <= set(r.get("injection_evidence_ids", []))) if ie else None
    d.update({"got_recommendation": r.get("recommendation"), "model_recommendation_before_correction": r.get("recommendation_model"),
              "quality_flags": r.get("quality_flags", [])})
    return s, d


def _tool_name(o):
    return (o.get("name") or "").split("-")[-1]


def tool_hits(o):
    """Hits returned by one preauth_policies / preauth_notes observation (MCP structured result)."""
    out = _json(o.get("output")) or {}
    res = (out.get("structuredContent") or {}).get("result") if isinstance(out, dict) else None
    if res is None and isinstance(out, dict):
        for c in out.get("content", []) or []:
            res = _json(c.get("text")) if isinstance(c, dict) else None
            if res is not None:
                break
    return res if isinstance(res, list) else []


def retrieval_metrics(obs, exp_retrieval, row):
    """What was REALLY pulled from Qdrant, read from the MCP tool observations of the agent trace."""
    pol_calls = [o for o in obs if o.get("type") == "TOOL" and _tool_name(o) == "preauth_policies"]
    note_calls = [o for o in obs if o.get("type") == "TOOL" and _tool_name(o) == "preauth_notes"]
    got_pol = {h.get("policy_id") for o in pol_calls for h in tool_hits(o)}
    got_notes = {f"{h.get('doc_id')}#{h.get('section')}" for o in note_calls for h in tool_hits(o)}
    s, d = {}, {"retrieved_policies": sorted(p for p in got_pol if p), "retrieved_notes": sorted(got_notes)}
    s["qdrant_calls_made"] = bool(pol_calls) and bool(note_calls)
    calls = pol_calls + note_calls
    ok = [c for c in calls if '"iserror": true' not in str(c.get("output", "")).lower() and c.get("level") != "ERROR"]
    s["qdrant_tool_success"] = (len(ok) == len(calls)) if calls else None
    exp_pol = exp_retrieval.get("expected_policy_ids", [])
    s["policy_retrieved_correct"] = all(p in got_pol for p in exp_pol) if exp_pol else None
    filt = False
    for o in pol_calls:
        a = _json(o.get("input")) or {}
        filt = filt or (a.get("as_of_date") == row.get("request_date") and a.get("plan_id") == row.get("plan_id"))
    s["policy_filters_correct"] = filt if pol_calls else False
    exp_notes = set(exp_retrieval.get("expected_note_ids", []))
    s["notes_recall_qdrant"] = _ratio(len(exp_notes & got_notes), len(exp_notes)) if exp_notes else None
    s["notes_precision_qdrant"] = _ratio(len(exp_notes & got_notes), len(got_notes)) if got_notes and exp_notes else None
    return s, d


def to_scores(s, details=None):
    """Drop None, type the rest. Returns [(name, value, dataType, comment)]."""
    out = []
    for name, v in s.items():
        if v is None or name not in SCORE_SPECS:
            continue
        dtype = SCORE_SPECS[name][0]
        val = (1 if v else 0) if dtype == "BOOLEAN" else float(v)
        comment = None
        if dtype == "BOOLEAN" and not v and details:
            comment = json.dumps({k: details[k] for k in details if details[k] and k in ("criterion_status_mismatch", "got_recommendation")}, default=str)[:600]
        out.append((name, val, dtype, comment))
    return out


# ------------------------------------------------------------------ one experiment item
def publish_item(exp, trace_id, root_id, start, end, name, input_, output, expected, metadata, scores, configs, extra_spans=()):
    """Send the item's root span (this is what makes the judges run) and post the code scores on it."""
    spans = [LF.root_span(trace_id, root_id, exp, name, start, end, input_, output, expected, metadata)] + list(extra_spans)
    LF.send_spans(spans)
    for n, v, t, c in scores:
        LF.post_score(trace_id, root_id, n, v, t, c, configs.get(n))


def run_case_item(exp_meta, cid, gt, rows, known, configs, wait_judges):
    exp = dict(exp_meta, item_id=cid)
    inp, expected, meta = build_item(gt, rows[cid])
    trace_id, root_id = LF.new_trace_ids()
    headers = {"traceparent": LF.traceparent(trace_id, root_id), "baggage": LF.baggage_header(exp)}
    run = run_agui(cid, headers)
    time.sleep(8)    # let the agent process flush its spans to Langfuse
    obs = LF.observations(trace_id, want_name="run_review")
    s, details = score_answer(gt, run["adjudication"], known)
    rs, rd = retrieval_metrics(obs, expected["retrieval"], rows[cid])
    s.update(rs)
    adj = run["adjudication"] or {}
    output = (adj.get("result") or {"error": run["error"] or run["review_error"] or "no result produced"})
    metadata = dict(meta, findings=json.dumps(adj.get("findings") or {}, default=str)[:30000], **rd)
    publish_item(exp, trace_id, root_id, run["start"], run["end"], f"experiment-item {cid}", inp, output, expected, metadata,
                 to_scores(s, details), configs)
    judges = LF.wait_for_judges(trace_id, LF.JUDGE_NAMES) if wait_judges and run["adjudication"] else {}
    return {"case_id": cid, "trace_id": trace_id, "root_id": root_id, "trace_url": LF.trace_url(trace_id),
            "latency_s": round(run["end"] - run["start"], 1), "scores": {n: v for n, v, _, _ in to_scores(s)},
            "judges": {n: {"value": j.get("value"), "comment": (j.get("comment") or "")[:300]} for n, j in judges.items()},
            "judges_missing": [n for n in LF.JUDGE_NAMES if n not in judges] if wait_judges else [],
            "got": details.get("got_recommendation"), "expected": gt["expected_recommendation"], "error": run["error"] or run["review_error"]}


def pick(ids, a):
    if a.cases and a.cases != "all":
        return [c.strip() for c in a.cases.split(",")]
    ids = sorted(ids)
    if a.sample:
        return sorted(random.Random(a.seed).sample(ids, min(a.sample, len(ids))))
    return ids


def save_manifest(name, data):
    os.makedirs(RESULTS, exist_ok=True)
    path = os.path.join(RESULTS, f"{name}.json")
    json.dump(data, open(path, "w"), indent=2, default=str)
    return path


# ------------------------------------------------------------------ commands
def cmd_setup(a):
    gt, rts = load_gt()
    rows = case_rows()
    print("1/5 Gemini connection (Google AI Studio) ...", end=" ", flush=True)
    LF.ensure_llm_connection()
    print("ok")
    print("2/5 datasets ...", end=" ", flush=True)
    ds = {LF.CASES_DATASET: LF.ensure_dataset(LF.CASES_DATASET, "Prior-auth review questions with the labelled answer, retrieval and reasoning"),
          LF.RETRIEVAL_DATASET: LF.ensure_dataset(LF.RETRIEVAL_DATASET, "Policy retrieval tests: the in-force version must win"),
          TRACES_DATASET: LF.ensure_dataset(TRACES_DATASET, "Existing traces turned into evaluation items")}
    print("ok")
    print("3/5 dataset items ...", end=" ", flush=True)
    for cid, exp in gt.items():
        inp, expected, meta = build_item(exp, rows[cid])
        LF.upsert_item(LF.CASES_DATASET, cid, inp, expected, meta)
    for t in rts:
        LF.upsert_item(LF.RETRIEVAL_DATASET, t["test_id"], {k: t[k] for k in ("query", "plan_id", "cpt", "request_date")},
                       {k: t[k] for k in t if k.startswith("must_") or k == "note"}, {"test_id": t["test_id"]})
    print(f"{len(gt)} cases + {len(rts)} retrieval items")
    print("4/5 score configs ...", end=" ", flush=True)
    LF.ensure_score_configs(SCORE_SPECS)
    print("ok")
    print("5/5 LLM judges (evaluators + rules) ...", end=" ", flush=True)
    j = LF.ensure_judges([ds[LF.CASES_DATASET]["id"], ds[TRACES_DATASET]["id"]])
    print(", ".join(x["judge"] for x in j))
    print("\nLangfuse is ready. Datasets:", LF.dataset_url(ds[LF.CASES_DATASET]["id"]))


def _experiment(a, dataset_name, desc):
    import uuid
    ds = LF.ensure_dataset(dataset_name, desc)
    return {"id": str(uuid.uuid4()), "name": a.name, "dataset_id": ds["id"], "description": desc}


def cmd_run(a):
    gt, _ = load_gt()
    rows, known = case_rows(), db_ids()
    configs = LF.ensure_score_configs(SCORE_SPECS)
    exp = _experiment(a, LF.CASES_DATASET, f"Prior-auth review run '{a.name}'")
    ids = pick(gt, a)
    manifest = {"name": a.name, "experiment_id": exp["id"], "dataset": LF.CASES_DATASET, "dataset_id": exp["dataset_id"],
                "started": dt.datetime.now(dt.timezone.utc).isoformat(), "items": []}
    for i, cid in enumerate(ids, 1):
        print(f"[{i}/{len(ids)}] {cid} ... running the app", flush=True)
        item = run_case_item(exp, cid, gt[cid], rows, known, configs, wait_judges=not a.no_judges)
        manifest["items"].append(item)
        print(f"     got {item['got']} (expected {item['expected']}) in {item['latency_s']}s  judges: "
              f"{ {k: v['value'] for k, v in item['judges'].items()} }  {item['trace_url']}", flush=True)
        if item["judges_missing"]:
            print(f"     (judges still pending in Langfuse: {item['judges_missing']})")
        save_manifest(a.name, manifest)
    print("\nDone. Langfuse > Experiments >", a.name, "|", LF.dataset_url(exp["dataset_id"]))
    print(f"Report: python3 pa_copilot_dataset/eval/run_eval.py report --name {a.name}")


def mcp_call(tool, args):
    tok = LF.env_value("ingestion_retriever/.env", "MCP_API_TOKEN")
    hdr = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

    def post(payload, sid=None):
        h = dict(hdr)
        if sid:
            h["mcp-session-id"] = sid
        r = urllib.request.urlopen(urllib.request.Request(QDRANT_MCP, data=json.dumps(payload).encode(), headers=h), timeout=90)
        raw = r.read().decode()
        body = next((l[5:] for l in raw.splitlines() if l.startswith("data:")), raw) if "data:" in raw else raw
        return r.headers.get("mcp-session-id"), (json.loads(body) if body.strip() else {})

    sid, _ = post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "eval", "version": "1"}}})
    post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    _, resp = post({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": args}}, sid)
    res = resp.get("result", {})
    sc = res.get("structuredContent")
    if sc is not None:
        return sc.get("result", sc)
    return json.loads("".join(c.get("text", "") for c in res.get("content", [])) or "[]")


def cmd_retrieval(a):
    import uuid
    gt, rts = load_gt()
    configs = LF.ensure_score_configs(SCORE_SPECS)
    exp = _experiment(a, LF.RETRIEVAL_DATASET, f"Policy retrieval run '{a.name}'")
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    manifest = {"name": a.name, "experiment_id": exp["id"], "dataset": LF.RETRIEVAL_DATASET, "dataset_id": exp["dataset_id"],
                "started": dt.datetime.now(dt.timezone.utc).isoformat(), "items": []}
    for t in rts:
        e = dict(exp, item_id=t["test_id"])
        trace_id, root_id = LF.new_trace_ids()
        t0 = time.time()
        args = {"query": t["query"], "top_k": 6, "plan_id": t["plan_id"], "cpt": t["cpt"]}
        try:
            c1s = time.time(); filt = mcp_call("preauth_policies", {**args, "as_of_date": t["request_date"]}); c1e = time.time()
            c2s = time.time(); unf = mcp_call("preauth_policies", args); c2e = time.time()
            err = None
        except Exception as ex:
            filt = unf = []; err = f"{type(ex).__name__}: {ex}"; c1s = c1e = c2s = c2e = time.time()
        fid = [h.get("policy_id") for h in filt]
        s = {}
        if "must_return_top" in t:
            s["top_is_in_force_version"] = (fid[0] if fid else None) == t["must_return_top"]
        if t.get("must_not_return"):
            s["excluded_versions_absent"] = not (set(fid) & set(t["must_not_return"]))
        for exc in t.get("must_not_apply", []):
            v = con.execute("SELECT valid_from, valid_to FROM exceptions WHERE exception_id=?", (exc,)).fetchone()
            s["expired_exception_not_applied"] = bool(v) and not ((v[0] is None or v[0] <= t["request_date"]) and (v[1] is None or t["request_date"] <= v[1]))
        lead = (unf[0].get("policy_id") if unf else None)
        s["semantic_leader_lost_to_date_filter"] = bool(lead and fid and lead != fid[0])
        output = {"date_filtered_top": fid[0] if fid else None, "date_filtered_ids": fid,
                  "without_date_filter": [(h.get("policy_id"), h.get("dense_score")) for h in unf], "error": err}
        kids = [LF.child_span(trace_id, uuid.uuid4().hex[:16], root_id, "preauth_policies (date-filtered)", c1s, c1e, {**args, "as_of_date": t["request_date"]}, fid, is_error=bool(err)),
                LF.child_span(trace_id, uuid.uuid4().hex[:16], root_id, "preauth_policies (no date filter)", c2s, c2e, args, [h.get("policy_id") for h in unf])]
        publish_item(e, trace_id, root_id, t0, time.time(), f"retrieval-item {t['test_id']}", {k: t[k] for k in ("query", "plan_id", "cpt", "request_date")},
                     output, {k: t[k] for k in t if k.startswith("must_")}, {"note": t.get("note", "")}, to_scores(s), configs, kids)
        manifest["items"].append({"case_id": t["test_id"], "trace_id": trace_id, "trace_url": LF.trace_url(trace_id), "scores": {n: v for n, v, _, _ in to_scores(s)},
                                  "got": output["date_filtered_top"], "expected": t.get("must_return_top")})
        print(f"{t['test_id']}: top={output['date_filtered_top']}  semantic leader without date filter={lead}  scores={ {n: v for n, v, _, _ in to_scores(s)} }")
    con.close()
    save_manifest(a.name, manifest)


def cmd_replay(a):
    """Evaluate EXISTING traces: pick (a sample of) past adjudications, link them as dataset items, re-score them
    from the stored trace, and send them as an experiment so the Langfuse judges run on them too."""
    import uuid
    gt, _ = load_gt()
    rows, known = case_rows(), db_ids()
    configs = LF.ensure_score_configs(SCORE_SPECS)
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=a.hours)).isoformat().replace("+00:00", "Z")
    seen_traces = set()
    for f in os.listdir(RESULTS) if os.path.isdir(RESULTS) else []:
        try:
            seen_traces |= {i["trace_id"] for i in json.load(open(os.path.join(RESULTS, f))).get("items", [])}
        except Exception:
            pass
    cand, cursor = [], None
    while len(cand) < 500:
        r = LF.api("GET", "/api/public/v2/observations", params={"name": "run_review", "type": "TOOL", "fromStartTime": since, "limit": 100,
                                                                 "cursor": cursor, "fields": "core,basic,io,time"})
        cand += r.get("data", [])
        cursor = (r.get("meta") or {}).get("cursor")
        if not cursor or not r.get("data"):
            break
    eligible = []
    for o in cand:
        args, out = _json(o.get("input")), _json(o.get("output"))
        cid = (args or {}).get("case_id") if isinstance(args, dict) else None
        result = (out or {}).get("result") if isinstance(out, dict) else None
        if cid in gt and result and o["traceId"] not in seen_traces:
            eligible.append({"trace_id": o["traceId"], "case_id": cid, "result": result})
    print(f"{len(cand)} past run_review calls in the last {a.hours:g}h, {len(eligible)} eligible (known case, has a result, not an earlier experiment)")
    if not eligible:
        return
    chosen = random.Random(a.seed).sample(eligible, min(a.sample or len(eligible), len(eligible)))
    ds = LF.ensure_dataset(TRACES_DATASET, "Existing traces turned into evaluation items")
    exp = {"id": str(uuid.uuid4()), "name": a.name, "dataset_id": ds["id"], "description": f"Replay of existing traces '{a.name}'"}
    manifest = {"name": a.name, "experiment_id": exp["id"], "dataset": TRACES_DATASET, "dataset_id": ds["id"], "items": []}
    for i, c in enumerate(chosen, 1):
        cid, old = c["case_id"], c["trace_id"]
        item_id = f"trace-{old[:12]}"
        inp, expected, meta = build_item(gt[cid], rows[cid])
        LF.upsert_item(TRACES_DATASET, item_id, inp, expected, dict(meta, source_trace_id=old), source_trace=old)
        old_obs = LF.observations(old, retries=2)
        s, details = score_answer(gt[cid], {"result": c["result"]}, known)
        rs, rd = retrieval_metrics(old_obs, expected["retrieval"], rows[cid])
        s.update(rs)
        findings = [{"tool": _tool_name(o), "input": str(o.get("input"))[:400], "output": str(o.get("output"))[:1500]}
                    for o in old_obs if o.get("type") == "TOOL" and _tool_name(o) in ("preauth_case_details", "preauth_notes", "preauth_policies", "preauth_policy_context")]
        trace_id, root_id = LF.new_trace_ids()
        e = dict(exp, item_id=item_id)
        now = time.time()
        publish_item(e, trace_id, root_id, now, now + 0.5, f"replay {cid}", inp, c["result"], expected,
                     dict(meta, source_trace_id=old, findings=json.dumps(findings)[:30000], **rd), to_scores(s, details), configs)
        judges = LF.wait_for_judges(trace_id, LF.JUDGE_NAMES) if not a.no_judges else {}
        print(f"[{i}/{len(chosen)}] {cid}  old trace {old[:12]}  got {details.get('got_recommendation')} expected {gt[cid]['expected_recommendation']}  "
              f"judges {({k: v.get('value') for k, v in judges.items()})}  {LF.trace_url(trace_id)}", flush=True)
        manifest["items"].append({"case_id": cid, "trace_id": trace_id, "source_trace_id": old, "trace_url": LF.trace_url(trace_id),
                                  "scores": {n: v for n, v, _, _ in to_scores(s)},
                                  "judges": {n: {"value": j.get("value"), "comment": (j.get("comment") or "")[:300]} for n, j in judges.items()},
                                  "got": details.get("got_recommendation"), "expected": gt[cid]["expected_recommendation"]})
    save_manifest(a.name, manifest)


def cmd_report(a):
    m = json.load(open(os.path.join(RESULTS, f"{a.name}.json")))
    agg, rows = {}, []
    for it in m["items"]:
        sc = {s["name"]: s for s in LF.scores_for_trace(it["trace_id"])}
        obs = LF.observations(it["trace_id"], retries=1)
        gens = [o for o in obs if o.get("type") == "GENERATION"]
        tin = sum((o.get("usageDetails") or {}).get("input", 0) or 0 for o in gens)
        tout = sum((o.get("usageDetails") or {}).get("output", 0) or 0 for o in gens)
        cost = sum(float(o.get("totalCost") or 0) for o in gens)
        for n, s in sc.items():
            if isinstance(s.get("value"), (int, float)):
                agg.setdefault(n, []).append(s["value"])
        rows.append((it, sc, len(gens), tin, tout, cost))
    link = LF.dataset_url(m["dataset_id"]) if m.get("dataset_id") else LF.LANGFUSE
    L = [f"# Evaluation report: `{a.name}`\n",
         f"Built from Langfuse by `run_eval.py report` ({dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}). Dataset `{m['dataset']}`; "
         f"open it in Langfuse: {link} (Experiments > {a.name}).\n",
         "Metrics are Langfuse scores: **code** metrics are computed by the runner (including what was really retrieved from Qdrant, read from the "
         "agent trace) and **judge** metrics (`reviewer_summary_groundedness`, `decision_support_boundary`, `reasoning_quality_vs_expected`) are "
         "LLM-as-judge evaluators that run inside Langfuse.\n", "## Averages over the items\n", "| Metric | Mean | n |", "|---|---|---|"]
    for n in sorted(agg):
        L.append(f"| {n} | {sum(agg[n]) / len(agg[n]):.2f} | {len(agg[n])} |")
    L += ["", "## Per item\n", "| Item | Expected | Got | Latency s | LLM calls | Tokens in/out | Cost USD | Trace |", "|---|---|---|---|---|---|---|---|"]
    for it, sc, g, ti, to, cost in rows:
        L.append(f"| {it['case_id']} | {it.get('expected', '')} | {it.get('got', '')} | {it.get('latency_s', '')} | {g} | {ti}/{to} | {cost:.4f} | [open]({it['trace_url']}) |")
    L += ["", "## Judge reasoning\n"]
    for it, sc, *_ in rows:
        for n in LF.JUDGE_NAMES:
            s = sc.get(n)
            if s:
                L.append(f"- **{it['case_id']} / {n}** = {s.get('value')}: {(s.get('comment') or '')[:400]}")
    L += ["", "## Limitations\n",
          "- The judges run on `gemini-3.5-flash-lite`, the same model family as the app, so they can share its blind spots; treat judge scores as a second opinion, not ground truth.",
          "- Code metrics compare against one labelled answer; a correct alternative citation counts as a miss.",
          "- Notes retrieval metrics only count what the agent pulled from Qdrant; evidence it read through the structured SQLite tool is not counted.",
          "- Small samples (a few items) show that the pipeline works; they are not a statistical estimate of accuracy."]
    path = os.path.join(HERE, "EVAL_REPORT.md")
    open(path, "w").write("\n".join(L) + "\n")
    print("\n".join(L[:22]), "\n...\nwrote", path)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup")
    for name in ("run", "retrieval", "replay", "report"):
        p = sub.add_parser(name)
        p.add_argument("--name", required=True, help="experiment name shown in Langfuse")
        if name in ("run", "replay"):
            p.add_argument("--cases", default="all")
            p.add_argument("--sample", type=int, default=0, help="random sample of N items")
            p.add_argument("--seed", type=int, default=1)
            p.add_argument("--no-judges", action="store_true", help="do not wait for the Langfuse judges")
        if name == "replay":
            p.add_argument("--hours", type=float, default=24)
    a = ap.parse_args()
    try:
        {"setup": cmd_setup, "run": cmd_run, "retrieval": cmd_retrieval, "replay": cmd_replay, "report": cmd_report}[a.cmd](a)
    except LF.LangfuseError as e:
        sys.exit(f"Langfuse error: {e}")


if __name__ == "__main__":
    main()
