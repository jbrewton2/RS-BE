from rag.service import _evidence_signal_score, _is_glossary_text

def test_evidence_signal_prefers_obligations():
    hi = "The Contractor shall implement RMF and comply with NIST 800-53."
    lo = "14.0 GLOSSARY & DEFINITIONS: Accountability means ..."
    assert _evidence_signal_score(hi) > _evidence_signal_score(lo)

def test_glossary_detection():
    assert _is_glossary_text("14.0 GLOSSARY & DEFINITIONS")
    assert _is_glossary_text("Definitions: Accountability means ...")
    assert not _is_glossary_text("The Contractor shall encrypt data at rest.")
