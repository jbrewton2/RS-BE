from rag.service import _backfill_sections_from_evidence, _strengthen_overview_from_evidence

def test_backfill_populates_empty_sections():
    sections = [
        {"id": "overview", "title": "OVERVIEW", "findings": [], "evidence": [{"doc":"x","docId":"x","text":"The Contractor shall implement RMF.", "charStart":0, "charEnd":10, "score":0.9}], "gaps": [], "recommended_actions": []},
        {"id": "scope-of-work", "title": "SCOPE OF WORK", "findings": [], "evidence": [], "gaps": [], "recommended_actions": []},
    ]
    out = _backfill_sections_from_evidence(sections, "risk_triage")
    out = _strengthen_overview_from_evidence(out)

    # overview should have findings
    ov = [s for s in out if s["id"] == "overview"][0]
    assert len(ov.get("findings") or []) > 0

    # empty section should get a gap/action, not hallucinated findings
    sow = [s for s in out if s["id"] == "scope-of-work"][0]
    assert len(sow.get("gaps") or []) > 0
    assert len(sow.get("recommended_actions") or []) > 0
