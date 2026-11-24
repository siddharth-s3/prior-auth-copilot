"""A2UI form submit -> stored reviewer data + a "review again" message (no model involved)."""
import asyncio

from agents.preauth.a2ui_actions import apply_a2ui_action

ACTION = {"name": "submit_missing_info", "context": {
    "case_id": "PA-004", "item_0": "Therapy duration", "value_0": "6 weeks PT", "item_1": "Therapy dates", "value_1": "  "}}


def payload(action=ACTION):
    return {"threadId": "t", "messages": [{"id": "1", "role": "user", "content": "Review case PA-004"}],
            "forwardedProps": {"a2uiAction": {"userAction": action}, "other": 1}}


def run(p, submit):
    return asyncio.run(apply_a2ui_action(p, submit))


def test_filled_fields_are_saved_and_review_again_requested():
    saved = []

    async def submit(case, item, value):
        saved.append((case, item, value))
        return {"supp_id": f"REV-00{len(saved)}"}

    out = run(payload(), submit)
    assert saved == [("PA-004", "Therapy duration", "6 weeks PT")]       # blank field skipped
    last = out["messages"][-1]
    assert last["role"] == "user" and "REV-001" in last["content"] and "Review case PA-004 again" in last["content"]
    assert "a2uiAction" not in out["forwardedProps"] and out["forwardedProps"]["other"] == 1
    assert len(out["messages"]) == 2


def test_store_unavailable_is_reported_not_hidden():
    async def submit(case, item, value):
        raise RuntimeError("review service unavailable")

    last = run(payload(), submit)["messages"][-1]["content"]
    assert "could not be saved" in last and "do not re-run" in last


def test_empty_form_saves_nothing():
    async def submit(*a):
        raise AssertionError("must not be called")

    empty = dict(ACTION, context={"case_id": "PA-004", "item_0": "x", "value_0": ""})
    assert "left every field empty" in run(payload(empty), submit)["messages"][-1]["content"]


def test_other_requests_pass_through_untouched():
    p = {"messages": [{"id": "1", "role": "user", "content": "hi"}], "forwardedProps": {}}
    assert run(p, None) is p
    other = payload({"name": "something_else", "context": {}})
    assert run(other, None) is other
