"""Handle A2UI form actions (the reviewer pressed a button inside an A2UI surface).

CopilotKit delivers the click as a new AG-UI run whose `forwardedProps.a2uiAction.userAction` carries the
action name and the values the reviewer typed. We (1) store the values as audited reviewer-supplied data
(REV-NNN) through the review MCP server, then (2) append a plain user message so the supervisor re-runs
the review, which now includes that data. The model never writes to the review store."""
import uuid

SUBMIT_MISSING_INFO = "submit_missing_info"


def extract_action(payload: dict):
    ua = ((payload.get("forwardedProps") or {}).get("a2uiAction") or {}).get("userAction")
    return ua if isinstance(ua, dict) else None


async def apply_a2ui_action(payload: dict, submit) -> dict:
    """Return the payload to run. `submit(case_id, item, value)` is an async callable that stores one
    supplement and returns {"supp_id": ...}; it may raise to signal the store is unavailable."""
    ua = extract_action(payload)
    if not ua or ua.get("name") != SUBMIT_MISSING_INFO:
        return payload
    ctx = ua.get("context") or {}
    case_id = str(ctx.get("case_id", ""))
    pairs = []
    i = 0
    while f"item_{i}" in ctx:
        value = str(ctx.get(f"value_{i}") or "").strip()
        if value:
            pairs.append((str(ctx[f"item_{i}"]), value))
        i += 1
    saved, errors = [], []
    for item, value in pairs:
        try:
            res = await submit(case_id, item, value)
            saved.append((res["supp_id"], item))
        except Exception as e:  # keep going; say exactly what was not saved
            errors.append(f"{item}: {e}")
    out = dict(payload)
    out["forwardedProps"] = {k: v for k, v in (payload.get("forwardedProps") or {}).items() if k != "a2uiAction"}
    if not pairs:
        text = f"I submitted the form for {case_id} but left every field empty. Nothing was saved."
    elif errors and not saved:
        text = (f"I tried to supply information for {case_id} but it could not be saved ({'; '.join(errors)}). "
                "Tell me it was not saved; do not re-run the review.")
    else:
        text = (f"I supplied information for {case_id}, now saved as reviewer-supplied data: " +
                "; ".join(f"{sid} ({item})" for sid, item in saved) +
                f". Review case {case_id} again now so it takes this into account." +
                (f" (Not saved: {'; '.join(errors)})" if errors else ""))
    out["messages"] = list(payload.get("messages") or []) + [
        {"id": str(uuid.uuid4()), "role": "user", "content": text}]
    return out
