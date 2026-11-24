"""The A2UI review screens come from a fixed catalog built in code; anything else is rejected."""
import pytest

from agents.preauth import a2ui_catalog as cat

RESULT = {"case_id": "PA-004", "recommendation": "MORE_INFORMATION_NEEDED", "applicable_policy_id": "POL-MRI-2026-V3",
          "criteria": [{"criterion_id": "C-01", "status": "SUPPORTED", "evidence_ids": ["NOTE-001#hpi"]},
                       {"criterion_id": "C-02", "status": "INSUFFICIENT_EVIDENCE", "evidence_ids": []}],
          "missing_information": ["Documented therapy duration", "Therapy start and end dates"],
          "quality_flags": ["example flag"], "verification": {"citations_valid": 1, "citations_total": 1}}


def surface_ids(ops):
    return [o["createSurface"]["surfaceId"] for o in ops if "createSurface" in o]


def test_checklist_contains_the_real_values():
    ops = cat.criteria_checklist(RESULT)
    blob = str(ops)
    assert "MORE INFORMATION NEEDED" in blob and "POL-MRI-2026-V3" in blob and "C-02" in blob
    assert "NOTE-001#hpi" in blob and "Citations verified: 1/1" in blob and "example flag" in blob


def test_form_has_one_field_per_missing_item_and_submit_action():
    ops = cat.missing_info_form("PA-004", RESULT["missing_information"])
    comps = next(o["updateComponents"]["components"] for o in ops if "updateComponents" in o)
    fields = [c for c in comps if c["component"] == "TextField"]
    assert [f["label"] for f in fields] == RESULT["missing_information"]
    btn = next(c for c in comps if c["component"] == "Button")
    ev = btn["action"]["event"]
    assert ev["name"] == "submit_missing_info" and ev["context"]["case_id"] == "PA-004"
    assert ev["context"]["value_1"] == {"path": "/form/f1"}


def test_form_only_when_information_is_missing():
    assert len(surface_ids(cat.review_surfaces(RESULT)["a2ui_operations"])) == 2
    done = dict(RESULT, missing_information=[])
    assert surface_ids(cat.review_surfaces(done)["a2ui_operations"]) == ["review-PA-004"]


def test_component_outside_catalog_is_rejected():
    with pytest.raises(cat.CatalogError, match="not in the fixed catalog"):
        cat.check_catalog([{"id": "root", "component": "Iframe", "src": "http://evil"}])


def test_missing_required_prop_and_dangling_child_rejected():
    with pytest.raises(cat.CatalogError):
        cat.check_catalog([{"id": "root", "component": "Button", "child": "x"}])
    with pytest.raises(cat.CatalogError):
        cat.check_catalog([{"id": "root", "component": "Column", "children": ["ghost"]}])


def test_same_input_gives_identical_layout():
    assert cat.review_surfaces(RESULT) == cat.review_surfaces(RESULT)
