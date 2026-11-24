"""Fixed A2UI catalog for the review screens: the UI is DATA built in code, never model-written.

Two surfaces, both built from the validated review result (see validate.py), so they always show the
real values and always have the same layout:
  * criteria_checklist(result)      recommendation badge, policy, one row per criterion, citation check
  * missing_info_form(case, items)  one text field per missing item + submit (action event)

Everything is checked against ALLOWED (a fixed component allow-list with required props) and the
toolkit's structural validator before it is emitted. A component outside the catalog is rejected."""
import json

from ag_ui_a2ui_toolkit import build_a2ui_envelope, validate_a2ui_components

# the fixed catalog: component name -> required properties (A2UI v0.9 basic catalog subset we allow)
ALLOWED = {
    "Column": ["children"], "Row": ["children"], "Card": ["child"], "Text": ["text"], "Divider": [],
    "Button": ["child", "action"], "TextField": ["label", "value"],
}
STATUS_MARK = {"SUPPORTED": "✓", "NOT_APPLICABLE": "–", "NOT_SUPPORTED": "✗",
               "INSUFFICIENT_EVIDENCE": "?", "CONFLICTING": "!"}


class CatalogError(ValueError):
    """A built surface used a component outside the fixed catalog or failed structural validation."""


def _text(cid, text, variant=None):
    c = {"id": cid, "component": "Text", "text": text}
    if variant:
        c["variant"] = variant
    return c


def check_catalog(components: list, data: dict | None = None) -> None:
    """Raise CatalogError unless every component is in ALLOWED with its required props and the
    surface is structurally valid (unique ids, resolvable child references, bindings)."""
    problems = []
    for c in components:
        name = c.get("component")
        if name not in ALLOWED:
            problems.append(f"{c.get('id')}: component {name!r} is not in the fixed catalog")
            continue
        problems += [f"{c.get('id')}: {name} is missing required property '{p}'"
                     for p in ALLOWED[name] if p not in c]
    res = validate_a2ui_components(components=components, data=data or {})
    if not res.get("valid", True):
        problems += [e.get("message", str(e)) for e in res.get("errors", [])]
    if problems:
        raise CatalogError("; ".join(problems))


def _ops(surface_id: str, components: list, data: dict | None = None) -> list:
    check_catalog(components, data)
    env = json.loads(build_a2ui_envelope(args={"surfaceId": surface_id, "components": components,
                                               "data": data or {}},
                                         is_update=False, target_surface_id=None, prior=None))
    return env["a2ui_operations"]


def criteria_checklist(result: dict) -> list:
    """A2UI operations for the checklist surface."""
    cid = result.get("case_id", "case")
    comps = [{"id": "root", "component": "Card", "child": "col"}]
    kids = ["title", "rec-row", "policy", "div1", "crit-h"]
    comps += [_text("title", f"Prior-authorization review: {cid}", "h2"),
              {"id": "rec-row", "component": "Row", "children": ["rec-l", "rec-v"]},
              _text("rec-l", "Recommendation (decision support, a person decides):", "caption"),
              _text("rec-v", str(result.get("recommendation", "")).replace("_", " "), "h3"),
              _text("policy", f"Governing policy: {result.get('applicable_policy_id') or 'none in force'}", "caption"),
              {"id": "div1", "component": "Divider"},
              _text("crit-h", "Criteria", "h3")]
    for i, c in enumerate(result.get("criteria", [])):
        rid = f"crit-{i}"
        ev = ", ".join(c.get("evidence_ids", [])) or "no verified evidence"
        comps += [{"id": rid, "component": "Row", "children": [f"{rid}-id", f"{rid}-st", f"{rid}-ev"]},
                  _text(f"{rid}-id", c.get("criterion_id", ""), "h3"),
                  _text(f"{rid}-st", f"{STATUS_MARK.get(c.get('status'), '')} {c.get('status', '')}"),
                  _text(f"{rid}-ev", ev, "caption")]
        kids.append(rid)
    v = result.get("verification") or {}
    if v:
        kids.append("verify")
        comps.append(_text("verify", f"Citations verified: {v.get('citations_valid', 0)}/{v.get('citations_total', 0)}"
                                     + ("  ·  recommendation corrected by precedence rule"
                                        if v.get("recommendation_corrected") else ""), "caption"))
    for i, flag in enumerate(result.get("quality_flags", [])[:4]):
        kids.append(f"flag-{i}")
        comps.append(_text(f"flag-{i}", f"⚠ {flag}", "caption"))
    comps.append({"id": "col", "component": "Column", "children": kids})
    return _ops(f"review-{cid}", comps)


def missing_info_form(case_id: str, items: list) -> list:
    """A2UI operations for the case-specific missing-documentation form (one field per missing item)."""
    items = [str(i) for i in items][:8]
    comps = [{"id": "root", "component": "Card", "child": "col"}]
    kids = ["title", "help"]
    comps += [_text("title", f"Information needed for {case_id}", "h3"),
              _text("help", "Supply what is missing. It is stored as reviewer-supplied data (REV-NNN), "
                            "audited, and used by the next review. It never edits the original record.", "caption")]
    ctx = {"case_id": case_id}
    data = {"form": {}}
    for i, item in enumerate(items):
        fid = f"f{i}"
        data["form"][fid] = ""
        comps.append({"id": fid, "component": "TextField", "label": item, "value": {"path": f"/form/{fid}"},
                      "variant": "longText"})
        kids.append(fid)
        ctx[f"item_{i}"] = item
        ctx[f"value_{i}"] = {"path": f"/form/{fid}"}
    comps += [_text("btn-t", "Submit to reviewer file"),
              {"id": "btn", "component": "Button", "child": "btn-t", "variant": "primary",
               "action": {"event": {"name": "submit_missing_info", "context": ctx}}}]
    kids.append("btn")
    comps.append({"id": "col", "component": "Column", "children": kids})
    return _ops(f"missing-info-{case_id}", comps, data)


def review_surfaces(result: dict) -> dict:
    """Envelope with the checklist, plus the form when information is missing."""
    ops = criteria_checklist(result)
    if result.get("missing_information"):
        ops += missing_info_form(result.get("case_id", "case"), result["missing_information"])
    return {"a2ui_operations": ops}
