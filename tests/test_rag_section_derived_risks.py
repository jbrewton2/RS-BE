import os
import sys
from typing import Any, Dict, List

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
        text = "\n".join(
            [
                "OVERVIEW",
                "This is an overview.",
                "SCOPE OF WORK",
                "The Contractor may provide services as appropriate.",
            ]
        )
        return {"text": text}


def test_section_derived_tier2_present():
    review_id = "r1"
    reviews = [{"id": review_id, "docs": [], "autoFlags": {"hits": [], "summary": {}, "hitsByDoc": {}, "explainReady": True}}]

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
        enable_inference_risks=True,
        inference_candidates=["Example inference candidate"],  # Tier1 is required; keep a non-empty list
    )

    stats = result.get("stats") or {}
    ro = (stats.get("risk_objects") or {})
    assert int(ro.get("tier2_sections", 0)) > 0

    risks = result.get("risks") or []
    assert any((r.get("source") == "sectionDerived") for r in risks)
