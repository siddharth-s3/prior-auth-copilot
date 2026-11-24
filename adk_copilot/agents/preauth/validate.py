"""Deterministic checks applied to the model's review result (no LLM involved).

1. normalize + JSON-schema validation of the whole result object
2. citation verification: every evidence id must exist in the case record (or the policy findings /
   reviewer-supplied data); unknown ids are removed and flagged, SUPPORTED with no valid evidence is
   downgraded to INSUFFICIENT_EVIDENCE
3. the recommendation is recomputed from the criterion statuses with the documented precedence rule;
   a model answer that disagrees is corrected and flagged

The model proposes; this module checks. All functions are pure so they are unit-tested."""
import json
import os
import re

from jsonschema import Draft7Validator

RECOMMENDATIONS = ("MEETS_CRITERIA", "MORE_INFORMATION_NEEDED", "DOES_NOT_APPEAR_TO_MEET_CRITERIA",
                   "SPECIALIST_REVIEW")
STATUSES = ("SUPPORTED", "NOT_SUPPORTED", "INSUFFICIENT_EVIDENCE", "CONFLICTING", "NOT_APPLICABLE")
# ids that are part of the policy text itself, not of the case record
POLICY_REFS = {"RF-01", "EX-01", "EX-02"}

with open(os.path.join(os.path.dirname(__file__), "result_schema.json")) as _f:
    _VALIDATOR = Draft7Validator(json.load(_f))


def parse_json(text):
    """Model output -> dict. Tolerates ```json fences and surrounding prose; None if nothing parses."""
    if isinstance(text, dict):
        return text
    if not isinstance(text, str):
        return None
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S) or re.search(r"(\{.*\})", text, re.S)
    try:
        out = json.loads(m.group(1)) if m else None
    except Exception:
        return None
    return out if isinstance(out, dict) else None


def schema_problems(result: dict) -> list:
    """Human-readable JSON-schema violations (empty list = conforms)."""
    return [f"{'/'.join(map(str, e.path)) or '(root)'}: {e.message}"
            for e in sorted(_VALIDATOR.iter_errors(result), key=lambda e: list(map(str, e.path)))]


def normalize(result: dict) -> dict:
    """Fill list fields the model left out so downstream code and the schema see a stable shape."""
    r = dict(result)
    for k in ("missing_information", "uncertainties", "injection_evidence_ids"):
        if not isinstance(r.get(k), list):
            r[k] = [] if r.get(k) in (None, "") else [str(r[k])]
    r.setdefault("applicable_policy_id", None)
    r.setdefault("prompt_injection_present", bool(r["injection_evidence_ids"]))
    crit = []
    for c in r.get("criteria") or []:
        if isinstance(c, dict):
            c = dict(c)
            ev = c.get("evidence_ids")
            c["evidence_ids"] = [str(x) for x in ev] if isinstance(ev, list) else ([str(ev)] if ev else [])
            crit.append(c)
    r["criteria"] = crit
    return r


def recompute_recommendation(criteria: list, applicable_policy_id) -> str:
    """Precedence from the ground rules: no governing policy or unresolved conflict -> SPECIALIST_REVIEW;
    any criterion affirmatively NOT_SUPPORTED -> DOES_NOT_APPEAR_TO_MEET_CRITERIA; else any
    INSUFFICIENT_EVIDENCE -> MORE_INFORMATION_NEEDED; else MEETS_CRITERIA."""
    statuses = [c.get("status") for c in criteria]
    if not applicable_policy_id or not statuses or "CONFLICTING" in statuses:
        return "SPECIALIST_REVIEW"
    if "NOT_SUPPORTED" in statuses:
        return "DOES_NOT_APPEAR_TO_MEET_CRITERIA"
    if "INSUFFICIENT_EVIDENCE" in statuses:
        return "MORE_INFORMATION_NEEDED"
    return "MEETS_CRITERIA"


def known_ids(bundle: dict, policy_findings=None, supplements=None) -> set:
    """Every id a citation may legitimately point at, built from the real case record."""
    ids = set(POLICY_REFS)
    for doc_id, sections in (bundle.get("note_sections") or {}).items():
        ids.add(doc_id)
        for s in sections or []:
            ids.add(f"{doc_id}#{s.get('section')}")
    for key, rows in bundle.items():
        if key == "note_sections":
            continue
        for row in (rows if isinstance(rows, list) else [rows]):
            if isinstance(row, dict):
                ids.update(str(v) for k, v in row.items() if k.endswith("_id") and isinstance(v, str))
    pf = policy_findings if isinstance(policy_findings, dict) else {}
    if pf.get("applicable_policy_id"):
        ids.add(pf["applicable_policy_id"])
    for k in (pf.get("rejected") or {}):
        ids.add(k)
    for c in pf.get("criteria") or []:
        if isinstance(c, dict) and c.get("criterion_id"):
            ids.add(c["criterion_id"])
            if pf.get("applicable_policy_id"):
                ids.add(f"{pf['applicable_policy_id']}#{c['criterion_id']}")
    for e in pf.get("exceptions_available") or []:
        if isinstance(e, dict) and e.get("exception_id"):
            ids.add(e["exception_id"])
    ids.update(s["supp_id"] for s in (supplements or []) if isinstance(s, dict) and s.get("supp_id"))
    return ids


def verify_citations(result: dict, allowed: set):
    """Return (result, flags, stats). Unknown evidence ids are removed; a SUPPORTED criterion left with
    no valid evidence becomes INSUFFICIENT_EVIDENCE (missing evidence is never proof either way)."""
    flags, total, valid = [], 0, 0
    out = dict(result)
    out["criteria"] = []
    for c in result.get("criteria", []):
        c = dict(c)
        good = []
        for ev in c.get("evidence_ids", []):
            total += 1
            if ev in allowed:
                valid += 1
                good.append(ev)
            else:
                flags.append(f"unverified citation removed: {ev} (criterion {c.get('criterion_id')})")
        c["evidence_ids"] = good
        if c.get("status") == "SUPPORTED" and not good:
            c["status"] = "INSUFFICIENT_EVIDENCE"
            flags.append(f"{c.get('criterion_id')}: SUPPORTED without a verifiable citation, "
                         "downgraded to INSUFFICIENT_EVIDENCE")
        out["criteria"].append(c)
    inj = out.get("injection_evidence_ids") or []
    out["injection_evidence_ids"] = [i for i in inj if i in allowed] if allowed else inj
    return out, flags, {"citations_total": total, "citations_valid": valid}


def apply_checks(result: dict, bundle: dict | None, policy_findings=None, supplements=None) -> dict:
    """Run every deterministic check; returns the corrected result with `quality_flags` and `verification`."""
    r = normalize(result)
    flags = list(r.get("quality_flags") or [])
    stats = {"citations_total": 0, "citations_valid": 0}
    if bundle:
        r, cflags, stats = verify_citations(r, known_ids(bundle, policy_findings, supplements))
        flags += cflags
    else:
        flags.append("citations could not be verified: case record unavailable")
    model_rec = r.get("recommendation")
    expected = recompute_recommendation(r["criteria"], r.get("applicable_policy_id"))
    corrected = model_rec != expected
    if corrected:
        flags.append(f"recommendation corrected by precedence rule: {model_rec!r} -> {expected}")
        r["recommendation_model"] = model_rec
        r["recommendation"] = expected
    r["requires_human_review"] = True
    problems = schema_problems(r)
    r["quality_flags"] = flags
    r["verification"] = {**stats, "schema_valid": not problems, "schema_problems": problems,
                         "recommendation_corrected": corrected}
    return r
