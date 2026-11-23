#!/usr/bin/env python3
"""Curates Synthea background-population fixtures from a Synthea run into fhir/synthea/.
Deterministic given the same Synthea output (fixed seed). Usage:
    python curate_synthea.py <synthea_output_fhir_dir> [dest_dir]

Synthea command used to produce the input (see README):
    ./gradlew build -x test   # once
    ./run_synthea -s 20261120 -p 40 -a 35-75 Massachusetts
"""
import json, glob, os, re, shutil, sys

SRC = sys.argv[1]
DEST = sys.argv[2] if len(sys.argv) > 2 else "fhir/synthea"
TARGET = 26
PAT = re.compile(r"back|lumbar|lumbago|sciatica|radicul", re.I)
# Bulk resource types removed from fixtures to keep the repo small (EOB + Provenance are ~90% of Synthea output size).
DROP = {"ExplanationOfBenefit", "Provenance", "SupplyDelivery", "ImagingStudy", "Media", "Device", "CoverageEligibilityRequest"}
CUTOFF = "2024-01-01"  # keep ~2 years of history in fixtures

DATE_PATHS = {
    "Observation": ("effectiveDateTime", ("effectivePeriod", "start")),
    "DiagnosticReport": ("effectiveDateTime", ("effectivePeriod", "start")),
    "Encounter": (("period", "start"),),
    "Condition": ("onsetDateTime", "recordedDate"),
    "Procedure": ("performedDateTime", ("performedPeriod", "start")),
    "MedicationRequest": ("authoredOn",),
    "MedicationAdministration": ("effectiveDateTime", ("effectivePeriod", "start")),
    "Claim": (("billablePeriod", "start"), "created"),
    "Immunization": ("occurrenceDateTime",),
    "CarePlan": (("period", "start"),),
    "CareTeam": (("period", "start"),),
    "DocumentReference": ("date",),
    "AllergyIntolerance": ("recordedDate",),
}


def res_date(r):
    for p in DATE_PATHS.get(r["resourceType"], ()):
        if isinstance(p, tuple):
            v = r.get(p[0]) or {}
            if v.get(p[1]): return v[p[1]]
        elif r.get(p):
            return r[p]
    return None


def slim(bundle):
    entries = [e for e in bundle["entry"]
               if e["resource"]["resourceType"] == "Patient"
               or (e["resource"]["resourceType"] not in DROP and (res_date(e["resource"]) or "") >= CUTOFF)]
    # fixpoint: drop entries that reference a fullUrl no longer present
    uuids = lambda es: {e["fullUrl"] for e in es}
    refs = lambda es: {r for e in es for r in re.findall(r'urn:uuid:[0-9a-f-]{36}', json.dumps(e["resource"]))}
    while True:
        have = uuids(entries)
        kept = [e for e in entries
                if e["resource"]["resourceType"] == "Patient"
                or all(not r.startswith("urn:uuid:") or r in have for r in re.findall(r'"reference": "([^"]+)"', json.dumps(e["resource"])))]
        if len(kept) == len(entries): break
        entries = kept
    return {**bundle, "entry": entries}

bundles = [f for f in sorted(glob.glob(os.path.join(SRC, "*.json")))
           if "hospitalInformation" not in f and "practitionerInformation" not in f]
# Slim first, then keep only bundles with a usable amount of recent history (20..1500 entries).
candidates = []
for f in bundles:
    full_txt = open(f).read()
    b = slim(json.loads(full_txt))
    if 20 <= len(b["entry"]) <= 1500:
        candidates.append((f, b, bool(PAT.search(full_txt.lower()))))
hits = [c for c in candidates if c[2]]
rest = [c for c in candidates if not c[2]]
picked = (hits + rest)[:TARGET]

os.makedirs(DEST, exist_ok=True)
for old in glob.glob(os.path.join(DEST, "synthea-pt-*.json")):
    os.remove(old)
summary = []
for i, (src, b, back_related) in enumerate(picked, 1):
    dest = os.path.join(DEST, f"synthea-pt-{i:03d}.json")
    with open(dest, "w") as f:
        json.dump(b, f, indent=2)
        f.write("\n")
    conds = [e["resource"]["code"]["coding"][0].get("display", "")
             for e in b["entry"] if e["resource"]["resourceType"] == "Condition"]
    summary.append({"file": os.path.basename(dest), "source": os.path.basename(src),
                    "entries": len(b["entry"]), "conditions": len(conds),
                    "back_related_in_full_record": back_related})
with open(os.path.join(DEST, "INDEX.json"), "w") as f:
    json.dump({"synthea_seed": 20261120, "count": len(picked), "history_cutoff": CUTOFF,
               "slimmed": sorted(DROP), "bundles": summary}, f, indent=2)
    f.write("\n")
print(f"wrote {len(picked)} bundles to {DEST} ({len([s for s in summary if s['back_related_in_full_record']])} back-related)")
