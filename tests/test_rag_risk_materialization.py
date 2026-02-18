import os
import sys
from typing import Any, Dict, List

# Ensure repo root is on sys.path for local runs and CI
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from rag.service import rag_analyze_review


class FakeStorage:
    def __init__(self, reviews: List[Dict[str, Any]]):
        self._reviews = reviews

    def get_object(self, key: str) -> bytes:
        if key == "stores/reviews.json":
            import json
            return json.dumps(self._reviews).encode("utf-8")
        raise KeyError(key)

    def put_object(self, *args, **kwargs):
        return None


class FakeVector:
    def query(self, *args, **kwargs):
        return []


class FakeLLM:
    def embed_texts(self, texts: List[str]):
        return [[0.0] * 8 for _ in texts]

    def generate(self, prompt: str, *args, **kwargs):
        # IMPORTANT: rag.service expects a dict with {"text": "..."}
        text = "\n".join(
            [
                "OVERVIEW",
                "Insufficient evidence retrieved for this section.",
                "MISSION & OBJECTIVE",
                "Insufficient evidence retrieved for this section.",
                "SCOPE OF WORK",
                "Insufficient evidence retrieved for this section.",
                "DELIVERABLES & TIMELINES",
                "Insufficient evidence retrieved for this section.",
                "SECURITY, COMPLIANCE & HOSTING CONSTRAINTS",
                "Insufficient evidence retrieved for this section.",
                "ELIGIBILITY & PERSONNEL CONSTRAINTS",
                "Insufficient evidence retrieved for this section.",
                "LEGAL & DATA RIGHTS RISKS",
                "Insufficient evidence retrieved for this section.",
                "FINANCIAL RISKS",
                "Insufficient evidence retrieved for this section.",
                "SUBMISSION INSTRUCTIONS & DEADLINES",
                "Insufficient evidence retrieved for this section.",
                "CONTRADICTIONS & INCONSISTENCIES",
                "Insufficient evidence retrieved for this section.",
                "GAPS / QUESTIONS FOR THE GOVERNMENT",
                "Insufficient evidence retrieved for this section.",
                "RECOMMENDED INTERNAL ACTIONS",
                "Insufficient evidence retrieved for this section.",
            ]
        )
        return {"text": text}


def test_rag_returns_risks_even_when_timing_enabled():
    os.environ["RAG_TIMING"] = "1"

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
        heuristic_hits=None,
        enable_inference_risks=False,
        inference_candidates=None,
    )

    assert isinstance(result, dict)
    assert result.get("review_id") == review_id
    assert "sections" in result
    assert "risks" in result

    risks = result.get("risks") or []
    assert isinstance(risks, list)
    assert len(risks) > 0, "Expected deterministic risks even when RAG_TIMING=1"
