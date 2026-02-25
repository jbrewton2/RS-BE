import pytest
from reviews import router as reviews_router


class _FakeStorage:
    def get_object(self, key: str):
        raise RuntimeError("Storage fallback should not be used in this test")


@pytest.mark.asyncio
async def test_readtime_rag_section_with_evidence_rewrites_findings(monkeypatch):
    review_id = "rev-test-1"
    doc_id = "doc-1"

    evidence = [{
        "docId": doc_id,
        "evidenceId": f"{doc_id}::1:0:10",
        "charStart": 0,
        "charEnd": 10,
        "score": 0.99,
        "text": "Offerors who have been favorably evaluated will be issued a Request for Proposal (RFP)."
    }]

    item = {
        "id": review_id,
        "review_id": review_id,
        "docs": [{"id": doc_id, "docId": doc_id, "title": "Doc 1"}],
        "aiRisks": [{
            "id": "rag-section:overview",
            "category": "RAG_SECTION",
            "title": "OVERVIEW",
            "description": "No findings returned.",
            "rationale": "No findings returned.",
            "evidence": evidence,
            "source": "sectionDerived",
            "source_type": "sectionDerived",
            "severity": "Medium",
        }],
        "sections": [{
            "id": "overview",
            "title": "OVERVIEW",
            "evidence": evidence,
        }],
    }

    class _FakeDynamoMeta:
        def get_review_detail(self, rid: str):
            assert rid == review_id
            return item

    monkeypatch.setattr(reviews_router, "DynamoMeta", _FakeDynamoMeta)

    out = await reviews_router.get_review(review_id=review_id, storage=_FakeStorage())

    rs = [r for r in (out.get("aiRisks") or []) if isinstance(r, dict) and r.get("category") == "RAG_SECTION" and r.get("title") == "OVERVIEW"]
    assert rs, "Expected to find OVERVIEW RAG_SECTION risk in output"
    r0 = rs[0]

    desc = str(r0.get("description") or "")
    assert desc.startswith("Findings:"), f"Expected Findings rewrite, got: {desc!r}"
    assert "\n- " in desc, f"Expected bullet formatting, got: {desc!r}"
    assert "Request for Proposal" in desc, "Expected evidence text to be reflected in Findings bullets"