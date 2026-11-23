#!/usr/bin/env python3
"""Builds pa_copilot.db (the transactional/SQLite half of the dataset) from the generated files.

Everything a direct ID pull answers lives here: patients, coverage, conditions, observations,
encounters, document references (with note section text), policies/metadata/criteria, exceptions,
cases, and authorization history. Policy/note text bodies that will feed vector search stay here
too as source of truth (chunking/embedding is a separate, postponed step).

Run after `python generate_dataset.py .` :  python build_sqlite.py [dataset_dir]
"""
import glob, json, os, re, sqlite3, sys

DS = sys.argv[1] if len(sys.argv) > 1 else "."
DB = os.path.join(DS, "pa_copilot.db")

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE plans (
  plan_id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT);
CREATE TABLE patients (
  patient_id TEXT PRIMARY KEY, mrn TEXT, given_name TEXT, family_name TEXT, gender TEXT, birth_date TEXT);
CREATE TABLE coverage (
  coverage_id TEXT PRIMARY KEY,
  patient_id TEXT NOT NULL REFERENCES patients(patient_id),
  plan_id TEXT NOT NULL REFERENCES plans(plan_id),
  status TEXT, payor TEXT, start_date TEXT, end_date TEXT);
CREATE TABLE conditions (
  condition_id TEXT PRIMARY KEY,
  patient_id TEXT NOT NULL REFERENCES patients(patient_id),
  code TEXT, description TEXT, onset_date TEXT, clinical_status TEXT);
CREATE TABLE encounters (
  encounter_id TEXT PRIMARY KEY,
  patient_id TEXT NOT NULL REFERENCES patients(patient_id),
  encounter_class TEXT, type TEXT, start_date TEXT, end_date TEXT);
CREATE TABLE observations (
  observation_id TEXT PRIMARY KEY,
  patient_id TEXT NOT NULL REFERENCES patients(patient_id),
  encounter_id TEXT NOT NULL REFERENCES encounters(encounter_id),
  code_system TEXT, code TEXT, display TEXT, value TEXT, unit TEXT, date TEXT);
CREATE TABLE document_references (
  doc_id TEXT PRIMARY KEY,
  patient_id TEXT NOT NULL REFERENCES patients(patient_id),
  encounter_id TEXT REFERENCES encounters(encounter_id),
  doc_type TEXT, date TEXT, author TEXT, status TEXT, url TEXT,
  relates_to_doc_id TEXT REFERENCES document_references(doc_id));
CREATE TABLE note_sections (
  doc_id TEXT NOT NULL REFERENCES document_references(doc_id),
  section TEXT NOT NULL, text TEXT NOT NULL,
  PRIMARY KEY (doc_id, section));
CREATE TABLE policies (
  policy_id TEXT PRIMARY KEY, family TEXT, title TEXT, version INTEGER,
  effective_start TEXT, effective_end TEXT,
  supersedes TEXT REFERENCES policies(policy_id),
  precedence TEXT, status_as_of TEXT, source_file TEXT, note TEXT);
CREATE TABLE policy_plans (
  policy_id TEXT NOT NULL REFERENCES policies(policy_id),
  plan_id TEXT NOT NULL REFERENCES plans(plan_id),
  PRIMARY KEY (policy_id, plan_id));
CREATE TABLE policy_criteria (
  policy_id TEXT NOT NULL REFERENCES policies(policy_id),
  criterion_id TEXT NOT NULL, section_ref TEXT, requirement TEXT,
  min_duration_days INTEGER, max_exam_age_days INTEGER, imaging_lookback_days INTEGER,
  PRIMARY KEY (policy_id, criterion_id));
CREATE TABLE exceptions (
  exception_id TEXT PRIMARY KEY, title TEXT, valid_from TEXT, valid_to TEXT,
  section_ref TEXT, condition TEXT, effect_json TEXT);
CREATE TABLE exception_policies (
  exception_id TEXT NOT NULL REFERENCES exceptions(exception_id),
  policy_id TEXT NOT NULL REFERENCES policies(policy_id),
  PRIMARY KEY (exception_id, policy_id));
CREATE TABLE cases (
  case_id TEXT PRIMARY KEY,
  patient_id TEXT NOT NULL REFERENCES patients(patient_id),
  plan_id TEXT NOT NULL REFERENCES plans(plan_id),
  request_date TEXT, scheduled_service_date TEXT,
  service_name TEXT, cpt TEXT, status TEXT, scenario TEXT);
CREATE TABLE auth_history (
  auth_id TEXT PRIMARY KEY,
  patient_id TEXT NOT NULL REFERENCES patients(patient_id),
  service TEXT, cpt TEXT, decision TEXT, decision_date TEXT, service_date TEXT,
  decided_by TEXT, note TEXT);
"""

def rid(ref_str):  # "Patient/pt-001" -> "pt-001"
    return ref_str.split("/", 1)[1] if ref_str else None

def main():
    if os.path.exists(DB): os.remove(DB)
    db = sqlite3.connect(DB)
    db.executescript(SCHEMA)
    ins = db.execute

    # ---- plans / policies / exceptions (structured, plain-JSON sources)
    plans = json.load(open(os.path.join(DS, "plans.json")))
    for p in plans:
        ins("INSERT INTO plans VALUES (?,?,?)", (p["plan_id"], p["name"], p["type"]))

    pindex = json.load(open(os.path.join(DS, "policies", "policy_index.json")))
    for p in pindex["policies"]:
        status = next((v for k, v in p.items() if k.startswith("status_as_of_")), None)
        ins("INSERT INTO policies VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
            p["policy_id"], p["family"], p["title"], p["version"], p["effective_start"], p["effective_end"],
            p["supersedes"], p["precedence"], status, p["source_file"], p.get("note")))
        for pl in p["plan_ids"]:
            ins("INSERT INTO policy_plans VALUES (?,?)", (p["policy_id"], pl))
        for cr in p["criteria"]:
            ins("INSERT INTO policy_criteria VALUES (?,?,?,?,?,?,?)", (
                p["policy_id"], cr["criterion_id"], cr["section_ref"], cr["requirement"],
                cr.get("min_duration_days"), cr.get("max_exam_age_days"), cr.get("imaging_lookback_days")))

    for x in json.load(open(os.path.join(DS, "exceptions.json"))):
        ins("INSERT INTO exceptions VALUES (?,?,?,?,?,?,?)", (
            x["exception_id"], x["title"], x["valid_from"], x["valid_to"], x["section_ref"],
            x["condition"], json.dumps(x["effect"])))
        for pid in x["policy_ids"]:
            ins("INSERT INTO exception_policies VALUES (?,?)", (x["exception_id"], pid))

    # ---- FHIR patient bundles -> relational rows
    for f in sorted(glob.glob(os.path.join(DS, "fhir", "pt-*.json"))):
        for e in json.load(open(f))["entry"]:
            r = e["resource"]; t = r["resourceType"]
            if t == "Patient":
                nm = r["name"][0]
                ins("INSERT INTO patients VALUES (?,?,?,?,?,?)", (
                    r["id"], r["identifier"][0]["value"], nm["given"][0], nm["family"], r["gender"], r["birthDate"]))
            elif t == "Condition":
                ins("INSERT INTO conditions VALUES (?,?,?,?,?,?)", (
                    r["id"], rid(r["subject"]["reference"]), r["code"]["coding"][0]["code"],
                    r["code"]["coding"][0].get("display"), r.get("onsetDateTime"),
                    r["clinicalStatus"]["coding"][0]["code"]))
            elif t == "Encounter":
                cls = r["class"]
                cls_txt = f"{cls['code']} ({cls['display']})" if cls.get("display") else cls["code"]
                ins("INSERT INTO encounters VALUES (?,?,?,?,?,?)", (
                    r["id"], rid(r["subject"]["reference"]), cls_txt,
                    r["type"][0]["text"], r["period"].get("start"), r["period"].get("end")))
            elif t == "Observation":
                coding = r["code"]["coding"][0]
                vq = r.get("valueQuantity", {})
                ins("INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?)", (
                    r["id"], rid(r["subject"]["reference"]), rid(r["encounter"]["reference"]),
                    coding.get("system"), coding.get("code"), coding.get("display") or r["code"].get("text"),
                    str(vq.get("value")) if vq else r.get("valueString"), vq.get("unit"), r["effectiveDateTime"]))
            elif t == "DocumentReference":
                ins("INSERT INTO document_references VALUES (?,?,?,?,?,?,?,?,?)", (
                    r["id"], rid(r["subject"]["reference"]), rid(r["context"]["encounter"][0]["reference"]),
                    r["type"]["text"], r["date"], r["author"][0]["display"], r["status"],
                    r["content"][0]["attachment"].get("url"),
                    rid(r["relatesTo"][0]["target"]["reference"]) if r.get("relatesTo") else None))
            elif t == "Coverage":
                cls = r["class"][0]
                ins("INSERT INTO coverage VALUES (?,?,?,?,?,?,?)", (
                    r["id"], rid(r["beneficiary"]["reference"]), cls["value"], r["status"],
                    r["payor"][0]["display"], r["period"].get("start"), r["period"].get("end")))

    # ---- note bodies -> section rows (keys match the NOTE-XXX#section citation anchors)
    for f in sorted(glob.glob(os.path.join(DS, "notes", "NOTE-*.md"))):
        doc_id = os.path.basename(f).replace(".md", "")
        body = open(f).read()
        body = body.split("---", 2)[-1]  # drop front matter
        parts = re.split(r"^## ", body, flags=re.M)
        for sec in parts[1:]:
            name, _, text = sec.partition("\n")
            key = name.strip().lower()  # "HPI" -> "hpi"; matches #hpi anchors
            ins("INSERT INTO note_sections VALUES (?,?,?)", (doc_id, key, text.strip()))
        if len(parts) == 1:
            raise SystemExit(f"no sections parsed from {f}")

    # ---- cases / auth history
    for c in json.load(open(os.path.join(DS, "cases.json"))):
        ins("INSERT INTO cases VALUES (?,?,?,?,?,?,?,?,?)", (
            c["case_id"], c["patient_id"], c["plan_id"], c["request_date"], c["scheduled_service_date"],
            c["requested_service"]["name"], c["requested_service"]["cpt"], c["status"], c["scenario"]))
    for a in json.load(open(os.path.join(DS, "authorization_history.json"))):
        ins("INSERT INTO auth_history VALUES (?,?,?,?,?,?,?,?,?)", (
            a["auth_id"], a["patient_id"], a["service"], a["cpt"], a["decision"],
            a["decision_date"], a["service_date"], a["decided_by"], a.get("note")))

    db.commit()

    # ================= ID / integrity checks =================
    errors = []
    def expect(cond, msg):
        if not cond: errors.append(msg)

    for err in db.execute("PRAGMA foreign_key_check").fetchall():
        errors.append(f"foreign key violation: {err}")

    PATTERNS = {"patients": r"pt-\d{3}$", "cases": r"PA-\d{3}$", "auth_history": r"AUTH-\d{3}$",
                "exceptions": r"EXC-\d{3}$", "document_references": r"NOTE-\d{3}$",
                "policies": r"POL-MRI(-MA|-CS)?-\d{4}-V\d$",
                "conditions": r"cond-\d{3}-\d+$", "encounters": r"enc-\d{3}-e\d+$",
                "observations": r"obs-\d{3}-\d+$", "coverage": r"cov-\d{3}$"}
    pk_cols = {"patients": "patient_id", "cases": "case_id", "auth_history": "auth_id",
               "exceptions": "exception_id", "document_references": "doc_id", "policies": "policy_id",
               "conditions": "condition_id", "encounters": "encounter_id",
               "observations": "observation_id", "coverage": "coverage_id"}
    for tbl, pat in PATTERNS.items():
        for (v,) in db.execute(f"SELECT {pk_cols[tbl]} FROM {tbl}"):
            expect(re.search(pat, v), f"{tbl}: bad id format {v}")

    counts = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ["plans", "patients", "coverage", "conditions", "encounters", "observations",
                        "document_references", "note_sections", "policies", "policy_criteria",
                        "exceptions", "cases", "auth_history"]}
    expect(counts["cases"] == db.execute("SELECT COUNT(*) FROM patients").fetchone()[0], "cases != patients (1 case per patient expected)")

    # every case must reference an existing patient and plan (also covered by FK) and its patient must
    # actually have the case's plan in coverage
    for cid, pid, plan in db.execute("SELECT case_id, patient_id, plan_id FROM cases"):
        got = db.execute("SELECT plan_id FROM coverage WHERE patient_id=?", (pid,)).fetchone()
        expect(got and got[0] == plan, f"{cid}: coverage plan {got} != case plan {plan}")

    # every ground-truth evidence id must resolve in the DB
    gt = json.load(open(os.path.join(DS, "eval", "ground_truth.json")))
    db_ids = {"NOTE": {r[0] for r in db.execute("SELECT doc_id FROM document_references")},
              "AUTH": {r[0] for r in db.execute("SELECT auth_id FROM auth_history")}}
    for cid, g in gt["cases"].items():
        evs = [e for cr in g["criteria"] for e in cr["evidence_ids"]] + g["injection_evidence_ids"]
        evs += [e for x in g["exceptions"] for e in x.get("evidence_ids", [])]
        for e in evs:
            nid, _, sec = e.partition("#")
            kind = nid.split("-")[0]
            expect(nid in db_ids[kind], f"{cid}: {e} not in {kind} table")
            if sec:
                n = db.execute("SELECT 1 FROM note_sections WHERE doc_id=? AND section=?", (nid, sec)).fetchone()
                expect(n, f"{cid}: {e} section missing in note_sections")
        pol = g["expected_applicable_policy_id"]
        if pol is not None:
            expect(db.execute("SELECT 1 FROM policies WHERE policy_id=?", (pol,)).fetchone(),
                   f"{cid}: expected policy {pol} not in policies")
        for rp in g["expected_rejected_policies"]:
            expect(rp == "UNVERIFIED-upload-0001" or db.execute("SELECT 1 FROM policies WHERE policy_id=?", (rp,)).fetchone(),
                   f"{cid}: rejected policy {rp} unknown")

    print("row counts:", json.dumps(counts, indent=1))
    if errors:
        print("ID/INTEGRITY ERRORS:"); [print(" -", e) for e in errors]; raise SystemExit(1)
    print("All ID and integrity checks passed. DB:", DB)

if __name__ == "__main__":
    main()
