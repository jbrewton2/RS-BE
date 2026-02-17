import os

from tests.test_rag_risk_materialization import FakeStorage, FakeVector, FakeLLM
from rag.service import rag_analyze_review


def test_risk_triage_context_includes_deterministic_signals_block_and_sources():
    os.environ["RAG_TIMING"] = "0"

    review_id = "r1"
    reviews = [
        {
            "id": review_id,
            "docs": [],
            "autoFlags": {
                "hits": [
                    {"id": "flag1", "label": "DFARS 7012", "severity": "High", "hit_key": "doc:flag:1:0"},
                ],
                "summary": {"counts": {"DFARS 7012": 1}},
                "hitsByDoc": {},
                "explainReady": True,
            },
        }
    ]

    heuristic_hits = [
        {"id": "heur1", "label": "Missing POP", "severity": "Medium", "why": "No period of performance found"},
    ]

    result = rag_analyze_review(
        storage=FakeStorage(reviews),
        vector=FakeVector(),
        llm=FakeLLM(),
        review_id=review_id,
        top_k=1,
        force_reingest=False,
        mode="review_summary",
        analysis_intent="risk_triage",
        context_profile="fast",
        debug=True,
        heuristic_hits=heuristic_hits,
        enable_inference_risks=False,
        inference_candidates=None,
    )

    # debug payload should include context for inspection
    ctx = (result.get("stats") or {}).get("debug_context") or ""
    assert "BEGIN DETERMINISTIC SIGNALS" in ctx
    assert "NOT CONTRACT EVIDENCE" in ctx

    # Must include autoFlag and heuristic indicators
    assert "DFARS 7012" in ctx
    assert "src=autoFlag" in ctx

    assert "Missing POP" in ctx
    assert "src=heuristic" in ctx
