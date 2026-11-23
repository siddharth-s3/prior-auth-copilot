"""Long-term memory tools (mem0 service). Memory is scoped per reviewer (ADK user_id), so it
persists across sessions and across `adk web` / A2A. Only memory_agent holds these tools."""
import os
import requests

MEM0_URL = os.environ.get("MEM0_URL", "http://host.docker.internal:8200").rstrip("/")
KINDS = ("preference", "override")   # no case outcomes: past results must not colour new reviews


def recall_memories(query: str, tool_context) -> dict:
    """Search this reviewer's long-term memory (preferences, past case outcomes, overrides,
    policy insights) for anything relevant to `query`.

    Args:
        query: what to look for, e.g. "formatting preferences" or "overrides".
    """
    try:
        r = requests.post(f"{MEM0_URL}/search", json={"query": query, "user_id": tool_context.user_id,
                                                       "limit": 5}, timeout=60)
        r.raise_for_status()
        return {"memories": [{"memory": m["memory"], "metadata": m.get("metadata"),
                              "score": round(m.get("score", 0), 3)} for m in r.json().get("results", [])]}
    except Exception as e:  # memory is best-effort: never block a review
        return {"memories": [], "error": f"memory service unavailable: {e}"}


def save_memory(text: str, kind: str, tool_context) -> dict:
    """Save a durable reviewer preference or override. An existing memory that says the same thing is
    replaced (no duplicates).

    Args:
        text: the fact, written as a self-contained sentence.
        kind: preference (how the reviewer wants output) | override (a standing reviewer rule that differs
            from the default handling).
    """
    if kind not in KINDS:
        return {"saved": False, "error": f"kind must be one of {KINDS}; case outcomes are never stored"}
    try:
        r = requests.post(f"{MEM0_URL}/upsert", json={"text": text, "user_id": tool_context.user_id,
                                                      "kind": kind}, timeout=90)
        r.raise_for_status()
        d = r.json()
        return {"saved": True, "replaced": [x["memory"] for x in d.get("replaced", [])]}
    except Exception as e:
        return {"saved": False, "error": f"memory service unavailable: {e}"}
