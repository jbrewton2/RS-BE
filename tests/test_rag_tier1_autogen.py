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
        # If this is the inference-engine prompt, return Tier1 candidates as bullets.
        if "Generate Tier-1 (inference) risk candidates" in (prompt or ""):
            return {"text": "\n".join([
                "- Missing clear acceptance criteria for deliverables",
                "- Period of performance may be undefined or unclear",
            ])}

        # Otherwise it's the main review summary prompt
        text = "\n".join(
            [
                "OVERVIEW",
                "This is an overview.",
                "SCOPE OF WORK",
                "Insufficient evidence retrieved for this section.",
            ]
        )
        return {"text": text}


def test_tier1_autogen_when_candidates_not_provided():
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
        inference_candidates=None,  # key: none provided -> should auto-gen
    )

    stats = result.get("stats") or {}
    ro = (stats.get("risk_objects") or {})
    assert int(ro.get("tier1_inference", 0)) > 0, "Expected Tier1 inference risks to be auto-generated"

    risks = result.get("risks") or []
    assert any((r.get("source") == "ai_only") for r in risks), "Expected at least one ai_only Tier1 risk"
