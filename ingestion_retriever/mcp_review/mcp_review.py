#!/usr/bin/env python3
"""Reviewer-action MCP server for the prior-auth copilot (the only WRITABLE data lane).

The case data stays read-only in mcp_sqlite. Everything a human reviewer does lives here, in its own
SQLite file: dispositions, clarification requests, supplied data, and an append-only audit log.

Tools:
  preauth_record_disposition(case_id, action, ai_recommendation, final_recommendation, reason, reviewer)
  preauth_request_clarification(case_id, question, reviewer)
  preauth_submit_supplement(case_id, item, value, reviewer)  -> id REV-NNN (reviewer-supplied data)
  preauth_get_review_state(case_id)                           -> status + latest actions
  preauth_list_review_states()                                -> {case_id: status} for the case list
  preauth_list_audit(case_id, limit)                          -> audit entries, newest first

This server is decision SUPPORT bookkeeping: it records what a person decided, it never decides.
Inputs are validated and parameterized (tool arguments are untrusted).
"""
import datetime, json, os, re, sqlite3
from fastmcp import FastMCP
from fastmcp.server.auth import StaticTokenVerifier

DB_PATH = os.environ.get("REVIEW_DB_PATH", "/data/review.db")
TOKEN = os.environ.get("MCP_API_TOKEN")
if not TOKEN:
    raise SystemExit("MCP_API_TOKEN is not set — refusing to start an unauthenticated server")

RECOMMENDATIONS = ("MEETS_CRITERIA", "MORE_INFORMATION_NEEDED", "DOES_NOT_APPEAR_TO_MEET_CRITERIA",
                   "SPECIALIST_REVIEW")
ACTIONS = {"ACCEPT": "ACCEPTED", "OVERRIDE": "OVERRIDDEN", "ESCALATE": "ESCALATED"}
CASE_RE = re.compile(r"^PA-\d{3}$")

mcp = FastMCP("preauth-review", auth=StaticTokenVerifier(
    tokens={TOKEN: {"client_id": "preauth-copilot", "scopes": ["read", "write"]}}))

SCHEMA = """
CREATE TABLE IF NOT EXISTS dispositions (id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL,
  action TEXT NOT NULL, ai_recommendation TEXT, final_recommendation TEXT NOT NULL, reason TEXT,
  reviewer TEXT NOT NULL, ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS clarifications (id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL,
  question TEXT NOT NULL, reviewer TEXT NOT NULL, ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS supplements (supp_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, item TEXT NOT NULL,
  value TEXT NOT NULL, reviewer TEXT NOT NULL, ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor TEXT NOT NULL,
  action TEXT NOT NULL, case_id TEXT NOT NULL, detail TEXT NOT NULL);
-- append-only: the audit log can never be edited or deleted through this server's SQL
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log BEGIN SELECT RAISE(ABORT,'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log BEGIN SELECT RAISE(ABORT,'audit_log is append-only'); END;
"""

def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con

def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

def _case(case_id):
    if not isinstance(case_id, str) or not CASE_RE.match(case_id):
        raise ValueError(f"invalid case_id {case_id!r} (expected PA-NNN)")
    return case_id

def _text(v, name, maxlen=1000, required=True):
    v = (v or "").strip()
    if required and not v:
        raise ValueError(f"{name} is required")
    if len(v) > maxlen:
        raise ValueError(f"{name} is too long (max {maxlen} characters)")
    return v

def _audit(con, actor, action, case_id, detail):
    con.execute("INSERT INTO audit_log (ts, actor, action, case_id, detail) VALUES (?,?,?,?,?)",
                (now(), actor, action, case_id, json.dumps(detail)))

def _status(con, case_id):
    """Latest review-relevant audit action decides the simulated case status."""
    row = con.execute("SELECT action, detail FROM audit_log WHERE case_id=? AND action IN "
                      "('DISPOSITION','CLARIFICATION_REQUESTED','SUPPLEMENT_SUBMITTED') ORDER BY id DESC LIMIT 1",
                      (case_id,)).fetchone()
    if not row:
        return "PENDING_REVIEW"
    if row["action"] == "CLARIFICATION_REQUESTED":
        return "CLARIFICATION_REQUESTED"
    if row["action"] == "SUPPLEMENT_SUBMITTED":
        return "INFO_SUPPLIED"
    return json.loads(row["detail"]).get("status", "PENDING_REVIEW")

@mcp.tool
def preauth_record_disposition(case_id: str, action: str, final_recommendation: str,
                               ai_recommendation: str = "", reason: str = "",
                               reviewer: str = "reviewer") -> dict:
    """Record the human reviewer's disposition for a case (with timestamp + audit entry).
    action: ACCEPT (reviewer agrees with the copilot), OVERRIDE (reviewer chooses a different
    recommendation — reason is mandatory) or ESCALATE (send to a specialist).
    final_recommendation must be one of MEETS_CRITERIA, MORE_INFORMATION_NEEDED,
    DOES_NOT_APPEAR_TO_MEET_CRITERIA, SPECIALIST_REVIEW. This records a person's decision about the
    COPILOT'S RECOMMENDATION; it is not a payer determination."""
    case_id = _case(case_id)
    action = (action or "").upper()
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {sorted(ACTIONS)}")
    if final_recommendation not in RECOMMENDATIONS:
        raise ValueError(f"final_recommendation must be one of {RECOMMENDATIONS}")
    if ai_recommendation and ai_recommendation not in RECOMMENDATIONS:
        raise ValueError("ai_recommendation is not a valid recommendation")
    reason = _text(reason, "reason", required=(action != "ACCEPT"))
    reviewer = _text(reviewer, "reviewer", 80)
    ts = now()
    with db() as con:
        con.execute("INSERT INTO dispositions (case_id, action, ai_recommendation, final_recommendation, reason, "
                    "reviewer, ts) VALUES (?,?,?,?,?,?,?)",
                    (case_id, action, ai_recommendation or None, final_recommendation, reason or None, reviewer, ts))
        _audit(con, reviewer, "DISPOSITION", case_id,
               {"action": action, "status": ACTIONS[action], "ai_recommendation": ai_recommendation or None,
                "final_recommendation": final_recommendation, "reason": reason or None})
    return {"case_id": case_id, "status": ACTIONS[action], "final_recommendation": final_recommendation, "ts": ts}

@mcp.tool
def preauth_request_clarification(case_id: str, question: str, reviewer: str = "reviewer") -> dict:
    """The reviewer asks for clarification (e.g. from the requesting provider) before deciding.
    Sets the simulated case status to CLARIFICATION_REQUESTED and writes an audit entry."""
    case_id = _case(case_id)
    question = _text(question, "question")
    reviewer = _text(reviewer, "reviewer", 80)
    ts = now()
    with db() as con:
        con.execute("INSERT INTO clarifications (case_id, question, reviewer, ts) VALUES (?,?,?,?)",
                    (case_id, question, reviewer, ts))
        _audit(con, reviewer, "CLARIFICATION_REQUESTED", case_id, {"question": question})
    return {"case_id": case_id, "status": "CLARIFICATION_REQUESTED", "ts": ts}

@mcp.tool
def preauth_submit_supplement(case_id: str, item: str, value: str, reviewer: str = "reviewer") -> dict:
    """The reviewer supplies or corrects a missing piece of information (for example the therapy
    duration). It is stored as reviewer-supplied data with a stable id (REV-NNN) that the next
    review can cite. It never edits the original clinical record."""
    case_id = _case(case_id)
    item = _text(item, "item", 300)
    value = _text(value, "value", 2000)
    reviewer = _text(reviewer, "reviewer", 80)
    ts = now()
    with db() as con:
        n = con.execute("SELECT COUNT(*) FROM supplements").fetchone()[0] + 1
        supp_id = f"REV-{n:03d}"
        con.execute("INSERT INTO supplements (supp_id, case_id, item, value, reviewer, ts) VALUES (?,?,?,?,?,?)",
                    (supp_id, case_id, item, value, reviewer, ts))
        _audit(con, reviewer, "SUPPLEMENT_SUBMITTED", case_id, {"supp_id": supp_id, "item": item, "value": value})
    return {"case_id": case_id, "supp_id": supp_id, "status": "INFO_SUPPLIED", "ts": ts}

@mcp.tool
def preauth_get_review_state(case_id: str) -> dict:
    """Review state of one case: simulated status, latest disposition, clarification requests and
    reviewer-supplied data (REV-NNN)."""
    case_id = _case(case_id)
    with db() as con:
        disp = con.execute("SELECT * FROM dispositions WHERE case_id=? ORDER BY id DESC LIMIT 1", (case_id,)).fetchone()
        return {"case_id": case_id, "status": _status(con, case_id),
                "latest_disposition": dict(disp) if disp else None,
                "clarifications": [dict(r) for r in con.execute(
                    "SELECT * FROM clarifications WHERE case_id=? ORDER BY id", (case_id,))],
                "supplements": [dict(r) for r in con.execute(
                    "SELECT * FROM supplements WHERE case_id=? ORDER BY supp_id", (case_id,))]}

@mcp.tool
def preauth_list_review_states() -> dict:
    """{case_id: simulated status} for every case that has had any reviewer action."""
    with db() as con:
        ids = [r[0] for r in con.execute("SELECT DISTINCT case_id FROM audit_log")]
        return {cid: _status(con, cid) for cid in ids}

@mcp.tool
def preauth_list_audit(case_id: str = "", limit: int = 50) -> list[dict]:
    """Audit trail (append-only), newest first. Leave case_id empty for all cases."""
    limit = max(1, min(int(limit), 500))
    with db() as con:
        if case_id:
            rows = con.execute("SELECT * FROM audit_log WHERE case_id=? ORDER BY id DESC LIMIT ?",
                               (_case(case_id), limit))
        else:
            rows = con.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) | {"detail": json.loads(r["detail"])} for r in rows]

if __name__ == "__main__":
    db().close()
    mcp.run(transport="http", host="0.0.0.0", port=8000)
