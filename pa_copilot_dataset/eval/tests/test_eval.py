"""Offline tests for the Langfuse evaluation code (no network).  Run: python3 -m unittest discover -s pa_copilot_dataset/eval/tests -t pa_copilot_dataset/eval"""
import json, os, re, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import langfuse_eval as LF
import run_eval as R

EXP = {"case_id": "PA-001", "scenario": "complete_case", "expected_applicable_policy_id": "POL-MRI-2026-V3",
       "expected_rejected_policies": {"POL-MRI-2025-V2": "superseded"}, "expected_recommendation": "MEETS_CRITERIA",
       "criteria": [{"criterion_id": "C-01", "status": "SUPPORTED", "evidence_ids": ["NOTE-001#hpi"]},
                    {"criterion_id": "C-03", "status": "SUPPORTED", "evidence_ids": ["NOTE-003#exam", "obs-001-3"]}],
       "missing_information": [], "uncertainties": [], "requires_human_review": True,
       "prompt_injection_present": False, "injection_evidence_ids": []}
ROW = {"plan_id": "PPO-GOLD", "request_date": "2026-08-12", "cpt": "72148"}


def tool(name, args, hits, level="DEFAULT"):
    return {"type": "TOOL", "name": name, "level": level, "input": json.dumps(args),
            "output": json.dumps({"content": [], "structuredContent": {"result": hits}, "isError": False})}


class DatasetItem(unittest.TestCase):
    def test_item_has_question_answer_retrieval_and_reasoning(self):
        inp, exp, meta = R.build_item(EXP, ROW)
        self.assertEqual(inp["question"], "Review case PA-001")
        self.assertEqual(exp["answer"]["recommendation"], "MEETS_CRITERIA")
        self.assertEqual(exp["retrieval"]["expected_policy_ids"], ["POL-MRI-2026-V3"])
        self.assertEqual(exp["retrieval"]["expected_note_ids"], ["NOTE-001#hpi", "NOTE-003#exam"])   # obs-... is not a note
        self.assertIn("POL-MRI-2025-V2", exp["retrieval"]["decoy_policy_ids"])
        self.assertIn("UNVERIFIED-upload-0001", exp["retrieval"]["decoy_policy_ids"])
        self.assertIn("C-01 is SUPPORTED", exp["reference_reasoning"])
        self.assertEqual(meta["request_date"], "2026-08-12")

    def test_no_policy_case_says_so(self):
        e = dict(EXP, expected_applicable_policy_id=None, expected_rejected_policies={})
        self.assertIn("no governing policy", R.reference_reasoning(e))
        self.assertEqual(R.build_item(e, ROW)[1]["retrieval"]["expected_policy_ids"], [])


class RetrievalMetrics(unittest.TestCase):
    def setUp(self):
        _, self.expected, _ = R.build_item(EXP, ROW)

    def test_correct_policy_filters_and_notes(self):
        obs = [tool("preauth_qdrant-preauth_policies", {"plan_id": "PPO-GOLD", "as_of_date": "2026-08-12", "cpt": "72148"}, [{"policy_id": "POL-MRI-2026-V3"}]),
               tool("preauth_qdrant-preauth_notes", {"patient_id": "pt-001"}, [{"doc_id": "NOTE-001", "section": "hpi"}, {"doc_id": "NOTE-002", "section": "plan"}])]
        s, d = R.retrieval_metrics(obs, self.expected["retrieval"], ROW)
        self.assertTrue(s["policy_retrieved_correct"] and s["policy_filters_correct"] and s["qdrant_calls_made"] and s["qdrant_tool_success"])
        self.assertEqual(s["notes_recall_qdrant"], 0.5)          # 1 of 2 expected sections
        self.assertEqual(s["notes_precision_qdrant"], 0.5)       # 1 of 2 retrieved sections is expected
        self.assertEqual(d["retrieved_policies"], ["POL-MRI-2026-V3"])

    def test_missing_date_filter_and_wrong_policy_are_caught(self):
        obs = [tool("preauth_qdrant-preauth_policies", {"plan_id": "PPO-GOLD"}, [{"policy_id": "POL-MRI-2025-V2"}]),
               tool("preauth_qdrant-preauth_notes", {"patient_id": "pt-001"}, [])]
        s, _ = R.retrieval_metrics(obs, self.expected["retrieval"], ROW)
        self.assertFalse(s["policy_retrieved_correct"])
        self.assertFalse(s["policy_filters_correct"])
        self.assertEqual(s["notes_recall_qdrant"], 0.0)
        self.assertIsNone(s["notes_precision_qdrant"])

    def test_no_qdrant_calls_at_all(self):
        s, _ = R.retrieval_metrics([], self.expected["retrieval"], ROW)
        self.assertFalse(s["qdrant_calls_made"])
        self.assertFalse(s["policy_filters_correct"])

    def test_tool_error_is_counted(self):
        obs = [tool("preauth_qdrant-preauth_policies", {}, [], level="ERROR"), tool("preauth_qdrant-preauth_notes", {}, [])]
        self.assertFalse(R.retrieval_metrics(obs, self.expected["retrieval"], ROW)[0]["qdrant_tool_success"])


class AnswerScores(unittest.TestCase):
    RESULT = {"recommendation": "MEETS_CRITERIA", "applicable_policy_id": "POL-MRI-2026-V3", "requires_human_review": True,
              "criteria": [{"criterion_id": "C-01", "status": "SUPPORTED", "evidence_ids": ["NOTE-001#hpi"]},
                           {"criterion_id": "C-03", "status": "NOT_SUPPORTED", "evidence_ids": ["NOTE-003#exam"]}],
              "missing_information": [], "verification": {"citations_valid": 2, "citations_total": 2, "schema_valid": True}}

    def test_status_mismatch_is_measured(self):
        s, d = R.score_answer(EXP, {"result": self.RESULT}, {"NOTE-001#hpi", "NOTE-003#exam"})
        self.assertTrue(s["policy_correct"] and s["recommendation_correct"] and s["citations_resolve_in_db"])
        self.assertEqual(s["criterion_status_accuracy"], 0.5)
        self.assertEqual(d["criterion_status_mismatch"], {"C-03": {"expected": "SUPPORTED", "got": "NOT_SUPPORTED"}})
        self.assertEqual(s["model_citation_validity"], 1.0)

    def test_decoy_policy_applied_is_flagged(self):
        bad = dict(self.RESULT, applicable_policy_id="POL-MRI-2025-V2")
        s, _ = R.score_answer(EXP, {"result": bad}, set())
        self.assertFalse(s["policy_correct"])
        self.assertFalse(s["decoy_policy_not_applied"])

    def test_no_result_is_one_failed_score(self):
        s, d = R.score_answer(EXP, None, set())
        self.assertEqual(s, {"produced_result": False})
        self.assertIsNone(d["got_recommendation"])

    def test_to_scores_types_and_skips_none(self):
        out = {n: (v, t) for n, v, t, _ in R.to_scores({"policy_correct": True, "evidence_recall": 0.5, "schema_valid": None, "unknown": 1})}
        self.assertEqual(out, {"policy_correct": (1, "BOOLEAN"), "evidence_recall": (0.5, "NUMERIC")})


class LangfusePayloads(unittest.TestCase):
    EXPM = {"id": "exp-1", "name": "demo", "dataset_id": "ds-1", "item_id": "PA-001"}

    def test_traceparent_and_baggage(self):
        tid, sid = LF.new_trace_ids()
        self.assertRegex(LF.traceparent(tid, sid), r"^00-[0-9a-f]{32}-[0-9a-f]{16}-01$")
        bag = LF.baggage_header(dict(self.EXPM, name="my run"))
        self.assertIn("langfuse.experiment.name=my%20run", bag)
        self.assertIn("langfuse.experiment.item.id=PA-001", bag)

    def test_root_span_has_the_documented_experiment_attributes(self):
        sp = LF.root_span("a" * 32, "b" * 16, self.EXPM, "item", 1.0, 2.0, {"q": 1}, {"r": 2}, {"e": 3}, {"findings": "x"})
        attrs = {a["key"]: a["value"] for a in sp["attributes"]}
        for k in ("langfuse.experiment.id", "langfuse.experiment.name", "langfuse.experiment.dataset.id", "langfuse.experiment.item.id",
                  "langfuse.experiment.item.root_observation_id", "langfuse.experiment.item.expected_output",
                  "langfuse.observation.input", "langfuse.observation.output", "langfuse.observation.metadata.findings"):
            self.assertIn(k, attrs)
        self.assertEqual(attrs["langfuse.experiment.item.root_observation_id"]["stringValue"], "b" * 16)
        self.assertNotIn("parentSpanId", sp)                         # must be the trace root
        self.assertEqual(json.loads(attrs["langfuse.observation.input"]["stringValue"]), {"q": 1})

    def test_judges_are_complete(self):
        self.assertEqual(len(LF.JUDGES), 3)
        for j in LF.JUDGES:
            text = " ".join(m["content"] for m in j["prompt"])
            used = set(re.findall(r"\{\{(\w+)\}\}", text))
            mapped = {m["variable"] for m in j["mapping"]}
            self.assertEqual(used, mapped, j["name"])                 # every prompt variable has exactly one mapping
            self.assertIn(j["output"]["dataType"], ("BOOLEAN", "NUMERIC"))
        self.assertEqual(LF.JUDGE_NAMES, ["reviewer_summary_groundedness", "decision_support_boundary", "reasoning_quality_vs_expected"])


if __name__ == "__main__":
    unittest.main()
