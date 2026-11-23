"""mem0 memory service for the prior-auth copilot (REST)."""
import os
from typing import Any, Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from mem0 import Memory

GW = os.environ.get("LITELLM_BASE_URL", "http://host.docker.internal:4000")
KEY = os.environ["LITELLM_MASTER_KEY"]

config = {
    "llm": {"provider": "openai", "config": {
        "model": "preauth-model", "openai_base_url": GW, "api_key": KEY, "temperature": 0.1}},
    "embedder": {"provider": "openai", "config": {
        "model": "preauth-embedding", "openai_base_url": GW, "api_key": KEY, "embedding_dims": 3072}},
    "vector_store": {"provider": "qdrant", "config": {
        "host": os.environ.get("QDRANT_HOST", "host.docker.internal"), "port": 6333,
        "collection_name": "mem0_preauth_memory", "embedding_model_dims": 3072}},
    "history_db_path": "/data/history.db",
}
mem = Memory.from_config(config)
app = FastAPI(title="mem0 preauth memory")


class Add(BaseModel):
    text: str
    user_id: str                    # the reviewer (long-term, cross-session)
    agent_id: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None   # e.g. {"kind": "case_outcome", "case_id": "PA-001"}
    infer: bool = True              # False = store verbatim (used for audit-style records)


class Search(BaseModel):
    query: str
    user_id: str
    limit: int = 5
    filters: Optional[dict[str, Any]] = None


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/memories")
def add(b: Add):
    return mem.add([{"role": "user", "content": b.text}], user_id=b.user_id,
                   agent_id=b.agent_id, metadata=b.metadata, infer=b.infer)


@app.post("/search")
def search(b: Search):
    return mem.search(b.query, user_id=b.user_id, limit=b.limit, filters=b.filters)


@app.get("/memories")
def list_all(user_id: str):
    return mem.get_all(user_id=user_id)


@app.delete("/memories/{memory_id}")
def delete(memory_id: str):
    mem.delete(memory_id)
    return {"deleted": memory_id}


ALLOWED_KINDS = ("preference", "override")   # deliberately NOT case outcomes: past results must not colour new reviews


class Upsert(BaseModel):
    text: str
    user_id: str
    kind: str                       # preference | override
    agent_id: str = "preauth"
    similarity: float = 0.85        # same-fact threshold: a reworded duplicate replaces the old memory


def _meta(m):
    return m.get("metadata") or {}


@app.post("/upsert")
def upsert(b: Upsert):
    """Store a reviewer preference/override, REPLACING any existing memory of the same kind that says
    (semantically) the same thing, so repeated or reworded statements never pile up."""
    if b.kind not in ALLOWED_KINDS:
        raise HTTPException(422, f"kind must be one of {ALLOWED_KINDS}")
    found = mem.search(b.text, user_id=b.user_id, limit=10).get("results", [])
    targets = [m for m in found if _meta(m).get("kind") == b.kind and m.get("score", 0) >= b.similarity]
    replaced = []
    for m in targets:
        mem.delete(m["id"])
        replaced.append({"id": m["id"], "memory": m["memory"]})
    added = mem.add([{"role": "user", "content": b.text}], user_id=b.user_id, agent_id=b.agent_id,
                    metadata={"kind": b.kind}, infer=False)
    return {"replaced": replaced, "added": added.get("results", [])}
