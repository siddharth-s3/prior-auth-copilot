#!/usr/bin/env python3
"""Folder-watching RAG ingestion: policies (md/json) + clinical notes (md) -> Qdrant.

Pipeline per file: parse -> chunk -> embed -> ingest. Each stage is tracked per file in
state/ingestion_state.json (sha256, per-stage status, last error), so a failed file can be
resumed with --retry-failed / --retry <path> without touching the rest.

Chunks are split per "## " sub-header via an explicit ==CHUNK== separator, never by size.
chunk_id doubles as the citation anchor: POL-MRI-2026-V3#2 / NOTE-001#hpi.

Env: GOOGLE_API_KEY, QDRANT_URL, WATCH_POLICIES, WATCH_NOTES, LOG_DIR, PARSED_DIR, STATE_FILE.
Modes: --watch (default), --once, --retry-failed, --retry <rel_path>, --recreate-collections.
"""
import argparse, hashlib, json, logging, os, re, sys, time, uuid, datetime as dt

import requests
from qdrant_client import QdrantClient
from qdrant_client.models import (Distance, VectorParams, SparseVectorParams, SparseVector,
                                  PointStruct, Filter, FilterSelector,
                                  FieldCondition, MatchValue, PayloadSchemaType)
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

SEP = "==CHUNK=="
NS = uuid.UUID("c2f3a8e1-9b4d-4e7a-b1c6-5d8f0a2e3b55")
MODELS = ["gemini-embedding-2", "gemini-embedding-001", "text-embedding-004"]
GEMINI = "https://generativelanguage.googleapis.com/v1beta/models/{model}:{op}"
BATCH = 40
STAGES = ("parse", "chunk", "embed", "ingest")
COLLECTIONS = {"policies": "prior_auth_policies", "notes": "prior_auth_notes"}
SKIP_FILES = {"policy_index.json", ".DS_Store"}
DOC_SUFFIXES = {".md", ".json", ".png", ".jpg", ".jpeg", ".pdf"}

CFG = dict(
    api_key=os.environ.get("GOOGLE_API_KEY", ""),
    qdrant=os.environ.get("QDRANT_URL", "http://localhost:6333"),
    policies=os.environ.get("WATCH_POLICIES", "../pa_copilot_dataset/policies"),
    notes=os.environ.get("WATCH_NOTES", "../pa_copilot_dataset/notes"),
    log_dir=os.environ.get("LOG_DIR", "log"),
    parsed=os.environ.get("PARSED_DIR", "parsed"),
    state_file=os.environ.get("STATE_FILE", "state/ingestion_state.json"),
)

# ---------------------------------------------------------------- logging
RUN_ID = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
os.makedirs(CFG["log_dir"], exist_ok=True)
log = logging.getLogger("ingest")
log.setLevel(logging.INFO)
_fh = logging.FileHandler(os.path.join(CFG["log_dir"], f"ingest-{RUN_ID}.log"))
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_fh); log.addHandler(logging.StreamHandler(sys.stdout))
JSONL = open(os.path.join(CFG["log_dir"], f"ingest-{RUN_ID}.jsonl"), "a")

def jlog(file, stage, level, message):
    JSONL.write(json.dumps({"run_id": RUN_ID, "file": file, "stage": stage,
                            "level": level, "message": message,
                            "ts": dt.datetime.now().isoformat()}) + "\n"); JSONL.flush()

# ---------------------------------------------------------------- state
class State:
    def __init__(self, path):
        self.path = path; os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.data = {"model": None, "dims": None, "files": {}}
        if os.path.exists(path):
            self.data.update(json.load(open(path)))
    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f: json.dump(self.data, f, indent=2)
        os.replace(tmp, self.path)
    def rec(self, rel):
        return self.data["files"].setdefault(rel, {"stages": {}, "error": None,
                                                   "point_ids": [], "sha256": None, "updated_at": None})

# ---------------------------------------------------------------- parsing
def parse_front_matter(text):
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    meta = {}
    if not m: return meta
    for line in m.group(1).splitlines():
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip()
        if v.startswith("[") and v.endswith("]"):
            v = [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
        elif v in ("null", "None", ""): v = None
        elif v.isdigit(): v = int(v)
        meta[k] = v
    return meta

def json_policy_to_md(doc):
    L = [f"# {doc['title']} ({doc['policy_id']})"]
    L.append(f"Version {doc['version']} | Effective {doc['effective_start']} to "
             f"{doc['effective_end'] or 'until superseded'} | Plans: {', '.join(doc['plan_ids'])}")
    for num, s in doc["sections"].items():
        L += ["", f"## {num}. {s['title']}"]
        if s.get("note"): L += ["", s["note"]]
        for st in s.get("statements", []): L += ["", st]
        for c in s.get("criteria", []): L += ["", f"- **{c['criterion_id']}** {c['text']}"]
    return "\n".join(L) + "\n"

def ocr_to_md(path):
    """PaddleOCR hook for image/PDF documents (lazy import; text files never hit this)."""
    try:
        from paddleocr import PaddleOCR
    except ImportError:
        raise RuntimeError("PaddleOCR not installed in this image")
    res = PaddleOCR(use_doc_orientation_classify=False, use_doc_unwarping=False).predict(path)
    lines = [t for page in res for t in page.get("rec_texts", [])]
    return "# OCR output ({0})\n\n".format(os.path.basename(path)) + "\n".join(lines) + "\n"

def add_separators(text):
    out = []
    for ln in text.splitlines():
        if ln.startswith("## "): out += ["", SEP, ""]
        out.append(ln)
    return "\n".join(out) + "\n"

# ---------------------------------------------------------------- chunking
def split_sections(text):
    return [p.strip() for p in text.split(SEP) if p.strip()]

def anchor_for_policy(policy_id, chunk):
    m = re.match(r"^## (\d+)\.", chunk)
    return f"{policy_id}#{m.group(1)}" if m else f"{policy_id}#meta"

def anchor_for_note(note_id, chunk):
    m = re.match(r"^## ([A-Za-z][\w ]*)", chunk)
    return f"{note_id}#{m.group(1).strip().lower()}" if m else f"{note_id}#meta"

# ---------------------------------------------------------------- embedding
def probe_model():
    for m in MODELS:
        try:
            vals = embed_batch(m, ["dimension probe"])[0]
            log.info(f"embedding model selected: {m} ({len(vals)} dims)")
            return m, len(vals)
        except Exception as e:
            log.warning(f"model {m} unavailable: {e}")
    raise RuntimeError(f"no usable embedding model among {MODELS}")

def _post(model, op, body, tries=5):
    url = GEMINI.format(model=model, op=op)
    for attempt in range(tries):
        try:
            r = requests.post(url, headers={"Content-Type": "application/json",
                                            "X-goog-api-key": CFG["api_key"]},
                              json=body, timeout=60)
            if r.status_code == 200: return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt * 3); continue  # free-tier 429 needs longer backoff
            raise RuntimeError(f"{model} {op} HTTP {r.status_code}: {r.text[:300]}")
        except requests.RequestException:
            if attempt == tries - 1: raise
            time.sleep(2 ** attempt * 2)
    raise RuntimeError(f"{model} {op} failed after {tries} tries: {r.text[:300] if r.ok is False else 'network'}")

def embed_batch(model, texts):
    body = {"requests": [{"model": f"models/{model}",
                          "content": {"parts": [{"text": t}]}} for t in texts]}
    out = _post(model, "batchEmbedContents", body)
    return [e["values"] for e in out["embeddings"]]

# ---------------------------------------------------------------- qdrant
def ensure_collections(client, dims, recreate=False):
    for c in COLLECTIONS.values():
        if recreate and client.collection_exists(c):
            client.delete_collection(c); log.warning(f"dropped collection {c} (--recreate-collections)")
        if client.collection_exists(c):
            v = client.get_collection(c).config.params.vectors
            got = v["dense"].size if isinstance(v, dict) else v.size
            if got != dims or not isinstance(v, dict):
                raise RuntimeError(f"collection {c} does not match expected shape (dense {dims} + sparse). "
                                   "Run with --recreate-collections to rebuild (drops all vectors).")
        else:
            client.create_collection(c,
                vectors_config={"dense": VectorParams(size=dims, distance=Distance.COSINE)},
                sparse_vectors_config={"sparse": SparseVectorParams()})
            log.info(f"created collection {c} (dense {dims} dims cosine + sparse BM25)")
        for key, schema in (("policy_id", PayloadSchemaType.KEYWORD), ("patient_id", PayloadSchemaType.KEYWORD),
                            ("chunk_id", PayloadSchemaType.KEYWORD), ("source_file", PayloadSchemaType.KEYWORD),
                            ("trusted", PayloadSchemaType.BOOL)):
            try: client.create_payload_index(c, key, schema)
            except Exception: pass  # already exists

def delete_file_points(client, collection, rel):
    client.delete(collection, points_selector=FilterSelector(filter=Filter(
        must=[FieldCondition(key="source_file", match=MatchValue(value=rel))])))

# ---------------------------------------------------------------- pipeline
class Ingestor:
    def __init__(self, client, model, dims, state):
        self.client, self.model, self.dims, self.state = client, model, dims, state
        self._sparse = None
        idx_path = os.path.join(CFG["policies"], "policy_index.json")
        self.policy_index = {}
        if os.path.exists(idx_path):
            for p in json.load(open(idx_path))["policies"]:
                self.policy_index[p["policy_id"]] = p

    @property
    def sparse(self):
        if self._sparse is None:
            from fastembed import SparseTextEmbedding
            self._sparse = SparseTextEmbedding("Qdrant/bm25")
        return self._sparse

    def collection_for(self, rel): return COLLECTIONS["notes" if rel.startswith("notes") else "policies"]

    def parse(self, path, rel):
        suf = os.path.splitext(rel)[1].lower()
        if suf in (".png", ".jpg", ".jpeg", ".pdf"): return ocr_to_md(path), {}
        if suf == ".json":
            doc = json.load(open(path))
            return json_policy_to_md(doc), {"policy_id": doc.get("policy_id"), "family": doc.get("family"),
                "version": doc.get("version"), "effective_start": doc.get("effective_start"),
                "effective_end": doc.get("effective_end"), "plan_ids": doc.get("plan_ids"),
                "supersedes": doc.get("supersedes"), "precedence": doc.get("precedence"), "format": "json"}
        text = open(path).read()
        return text, parse_front_matter(text) | {"format": "markdown"}

    def build_chunks(self, rel, text, meta):
        chunks = split_sections(add_separators(text))
        out = []
        if rel.startswith("notes"):
            nid = meta.get("note_id") or os.path.splitext(os.path.basename(rel))[0]
            for ch in chunks:
                cid = anchor_for_note(nid, ch)
                out.append({"chunk_id": cid, "text": ch, "payload": {
                    "chunk_id": cid, "doc_kind": "note", "doc_id": nid, "patient_id": meta.get("patient_id"),
                    "doc_type": meta.get("doc_type"), "date": meta.get("encounter_date"),
                    "author": meta.get("author"), "section": cid.split("#", 1)[1], "text": ch,
                    "source_file": rel, "trusted": True, "format": meta.get("format", "markdown")}})
        else:
            pid = meta.get("policy_id") or os.path.splitext(os.path.basename(rel))[0]
            idx = self.policy_index.get(pid, {})
            trusted = pid in self.policy_index and meta.get("status") != "DRAFT-UNVERIFIED"
            # one chunk per policy: the whole document (sections remain visible as ## headers in text)
            whole = re.sub(rf"\n*{SEP}\n*", "\n", text).strip()
            ts = lambda s: dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp().__int__() if s else None
            out.append({"chunk_id": pid, "text": whole, "payload": {
                "chunk_id": pid, "doc_kind": "policy", "policy_id": pid, "family": meta.get("family"),
                "version": meta.get("version"), "effective_start": meta.get("effective_start"),
                "effective_end": meta.get("effective_end"),
                # epoch copies enable range (version-aware date) filters in Qdrant; 4102444800 = year 2100 sentinel for open-ended
                "effective_start_ts": ts(meta.get("effective_start")) or 0,
                "effective_end_ts": ts(meta.get("effective_end")) or 4102444800,
                "plan_ids": meta.get("plan_ids"),
                "supersedes": meta.get("supersedes"), "precedence": meta.get("precedence"),
                "cpt_codes": idx.get("service", {}).get("cpt_codes"), "format": meta.get("format"),
                "section": "full", "source_file": rel, "trusted": trusted, "text": whole}})
        return out

    def process(self, path):
        # both watch roots share a parent (/watch), so this yields "policies/x" or "notes/x"
        rel = os.path.relpath(path, os.path.dirname(os.path.normpath(CFG["policies"])))
        base = os.path.splitext(os.path.basename(rel))[0]
        if os.path.basename(rel) in SKIP_FILES: return True
        rec = self.state.rec(rel)
        sha = hashlib.sha256(open(path, "rb").read()).hexdigest()
        if rec["sha256"] == sha and all(rec["stages"].get(s) == "ok" for s in STAGES):
            return True  # unchanged
        rec["stages"], rec["error"] = {}, None
        try:
            stage = "parse"
            text, meta = self.parse(path, rel)
            parsed_md = add_separators(text)
            with open(os.path.join(CFG["parsed"], f"{base}.parsed.md"), "w") as f: f.write(parsed_md)
            rec["stages"][stage] = "ok"; self.mark(rel, stage, "ok")

            stage = "chunk"
            chunks = self.build_chunks(rel, text, meta)
            if not chunks: raise RuntimeError("no chunks produced")
            with open(os.path.join(CFG["parsed"], f"{base}.chunks.json"), "w") as f:
                json.dump(chunks, f, indent=2)
            rec["stages"][stage] = "ok"; self.mark(rel, stage, "ok")

            stage = "embed"
            vectors = []
            for i in range(0, len(chunks), BATCH):
                vectors += embed_batch(self.model, [c["text"] for c in chunks[i:i + BATCH]])
            rec["stages"][stage] = "ok"; self.mark(rel, stage, "ok")

            stage = "ingest"
            coll = self.collection_for(rel)
            delete_file_points(self.client, coll, rel)  # CRUD: replace-old-then-upsert
            spars = list(self.sparse.embed([c["text"] for c in chunks]))
            points = [PointStruct(id=str(uuid.uuid5(NS, c["chunk_id"])),
                                  vector={"dense": v, "sparse": SparseVector(indices=s.indices.tolist(),
                                                                             values=s.values.tolist())},
                                  payload=c["payload"])
                      for c, v, s in zip(chunks, vectors, spars)]
            self.client.upsert(coll, points=points)
            rec["point_ids"] = [str(p.id) for p in points]
            rec["stages"][stage] = "ok"; self.mark(rel, stage, "ok")
            rec.update(sha256=sha, updated_at=dt.datetime.now().isoformat(), model=self.model)
            self.state.save()
            log.info(f"OK {rel}: {len(chunks)} chunks -> {coll}")
            return True
        except Exception as e:
            rec["stages"][stage] = "failed"
            rec["error"] = {"stage": stage, "message": str(e), "ts": dt.datetime.now().isoformat()}
            self.state.save()
            log.error(f"FAILED {rel} at {stage}: {e}")
            jlog(rel, stage, "error", str(e))
            return False

    def mark(self, rel, stage, status):
        jlog(rel, stage, "info", status)

    def delete(self, rel):
        rec = self.state.rec(rel)
        delete_file_points(self.client, self.collection_for(rel), rel)
        self.state.data["files"].pop(rel, None); self.state.save()
        log.info(f"DELETED {rel}: points removed")
        jlog(rel, "ingest", "info", "deleted")

    def sweep_and_retry(self, only_failed=False):
        ok = True
        for root in (CFG["policies"], CFG["notes"]):
            for name in sorted(os.listdir(root)):
                path = os.path.join(root, name)
                if not os.path.isfile(path) or name in SKIP_FILES: continue
                if os.path.splitext(name)[1].lower() not in DOC_SUFFIXES: continue
                rel = os.path.join(os.path.basename(root), name)
                rec = self.state.data["files"].get(rel)
                if only_failed:
                    if not (rec and (rec.get("error") or "failed" in rec.get("stages", {}).values())): continue
                    ok = self.process(path) and ok; continue
                sha = hashlib.sha256(open(path, "rb").read()).hexdigest()
                if rec and rec["sha256"] == sha and not rec.get("error"): continue
                ok = self.process(path) and ok
        if not only_failed:
            # prune files that disappeared from disk
            for rel in list(self.state.data["files"]):
                root = CFG["policies"] if rel.startswith("policies/") else CFG["notes"]
                if not os.path.exists(os.path.join(root, os.path.basename(rel))):
                    self.delete(rel)
        return ok

class Handler(FileSystemEventHandler):
    def __init__(self, ing): super().__init__(); self.ing = ing
    def on_created(self, e): self._go(e)
    def on_modified(self, e): self._go(e)
    def on_deleted(self, e): self._go(e, deleted=True)
    def _go(self, e, deleted=False):
        name = os.path.basename(e.src_path)
        if e.is_directory or name in SKIP_FILES: return
        if os.path.splitext(name)[1].lower() not in DOC_SUFFIXES: return
        rel = os.path.join(os.path.basename(os.path.dirname(e.src_path)), name)
        time.sleep(0.7)  # debounce editor save bursts
        try:
            self.ing.delete(rel) if deleted else self.ing.process(e.src_path)
        except Exception as ex:
            log.error(f"watcher error {rel}: {ex}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true", default=True)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--retry", metavar="REL_PATH")
    ap.add_argument("--recreate-collections", action="store_true")
    args = ap.parse_args()
    if not CFG["api_key"]: sys.exit("GOOGLE_API_KEY not set")

    state = State(CFG["state_file"])
    client = QdrantClient(url=CFG["qdrant"], timeout=60)
    model = state.data.get("model"); dims = state.data.get("dims")
    if not model:
        model, dims = probe_model()
        state.data.update(model=model, dims=dims); state.save()
    ensure_collections(client, dims, recreate=args.recreate_collections)
    log.info(f"model={model} dims={dims} qdrant={CFG['qdrant']}")
    ing = Ingestor(client, model, dims, state)

    if args.retry:
        for root, name in ((CFG["policies"], "policies"), (CFG["notes"], "notes")):
            if args.retry.startswith(name + "/"):
                path = os.path.join(root, os.path.basename(args.retry))
                if not os.path.exists(path): sys.exit(f"no such file: {args.retry}")
                sys.exit(0 if ing.process(path) else 1)
        sys.exit(f"rel path must start with policies/ or notes/: {args.retry}")
    if args.retry_failed:
        ok = ing.sweep_and_retry(only_failed=True)
        sys.exit(0 if ok else 1)
    ok = ing.sweep_and_retry()
    if args.once: sys.exit(0 if ok else 1)

    log.info("watching for changes... (Ctrl+C to stop)")
    obs = Observer()
    obs.schedule(Handler(ing), CFG["policies"], recursive=False)
    obs.schedule(Handler(ing), CFG["notes"], recursive=False)
    obs.start()
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        obs.stop()
    obs.join()

if __name__ == "__main__":
    main()
