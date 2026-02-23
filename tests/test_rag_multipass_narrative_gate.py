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


class CountingLLM:
    def __init__(self):
        self.calls = 0

    def embed_texts(self, texts: List[str]):
        return [[0.0] * 8 for _ in texts]

    def generate(self, prompt: str, *args, **kwargs):
        self.calls += 1
        # Return a minimal two-section summary format
        return {"text": "\n".join(["OVERVIEW", "ok", "SCOPE OF WORK", "ok"])}


def test_multipass_narrative_runs_only_in_deep_when_flag_on():
    os.environ["RAG_MULTIPASS_NARRATIVE"] = "1"

    review_id = "r1"
    reviews = [{"id": review_id, "docs": [], "autoFlags": {"hits": [], "summary": {}, "hitsByDoc": {}, "explainReady": True}}]

    # FAST profile: should NOT multipass; expect single generate call (prompt-based)
    llm_fast = CountingLLM()
    rag_analyze_review(
        storage=FakeStorage(reviews),
        vector=FakeVector(),
        llm=llm_fast,
        review_id=review_id,
        top_k=1,
        force_reingest=False,
        mode="review_summary",
        analysis_intent="risk_triage",
        context_profile="fast",
        debug=False,
        heuristic_hits=None,
        enable_inference_risks=True,
        inference_candidates=["x"],  # prevent Tier1 autogen extra calls
    )
    assert llm_fast.calls == 1

    # DEEP profile: multipass active; should call generate more than once
    llm_deep = CountingLLM()
    rag_analyze_review(
        storage=FakeStorage(reviews),
        vector=FakeVector(),
        llm=llm_deep,
        review_id=review_id,
        top_k=1,
        force_reingest=False,
        mode="review_summary",
        analysis_intent="risk_triage",
        context_profile="deep",
        debug=False,
        heuristic_hits=None,
        enable_inference_risks=True,
        inference_candidates=["x"],  # prevent Tier1 autogen extra calls
    )
    assert llm_deep.calls > 1
