#!/usr/bin/env python3
"""Seed agent prompts from prompts.py into Langfuse via REST (no SDK).
Run: docker run --rm --env-file .env -v "$PWD/scripts:/app/scripts:ro" adk_copilot-adk python /app/scripts/seed_prompts.py"""
import os, sys
os.environ["SKIP_LANGFUSE_PROMPTS"] = "1"   # read local prompts.py, not Langfuse
import requests as rq

sys.path.insert(0, "/app")
from agents.preauth import prompts as local

host = os.environ.get("LANGFUSE_HOST", "http://host.docker.internal:3000").rstrip("/")
auth = (os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"])

NAMES = {"supervisor": local.SUPERVISOR, "clinical_evidence": local.CLINICAL_EVIDENCE,
         "policy_rag": local.POLICY_RAG, "coverage_eligibility": local.COVERAGE_ELIGIBILITY,
         "criteria_quality": local.CRITERIA_QUALITY, "memory": local.MEMORY, "ground_rules": (local.__doc__ or "").strip()}

for slug, text in NAMES.items():
    r = rq.post(f"{host}/api/public/v2/prompts/",
                json={"name": f"preauth/{slug}", "type": "text", "prompt": text,
                      "labels": ["production"], "tags": ["preauth", "adk"]},
                auth=auth, timeout=15)
    r.raise_for_status()
    print(f"seeded preauth/{slug}")
print("done — prompts are editable/label-versioned in Langfuse (Prompts -> preauth/*)")
