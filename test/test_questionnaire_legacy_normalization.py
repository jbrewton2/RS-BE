import json
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.auth.jwt import get_current_user


@pytest.fixture(autouse=True)
def override_auth():
    """
    Override Depends(get_current_user) at the app level.
    """
    app.dependency_overrides[get_current_user] = lambda: {"sub": "test-user"}
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture(autouse=True)
def fake_storage(monkeypatch):
    """
    Patch the storage provider at the point FastAPI actually uses it.

    IMPORTANT:
    - Your Dockerfile copies the whole repo into /app/backend
    - So sessions_router is imported as: backend.questionnaire.sessions_router
    - That module binds get_providers at import time:
        from backend.providers.factory import get_providers
    Therefore we must patch:
        backend.questionnaire.sessions_router.get_providers
    """

    class FakeStorage:
        def __init__(self):
            self.data = {}

        def get_object(self, key: str) -> bytes:
            return self.data.get(key, b"[]")

        def put_object(self, key: str, data: bytes, content_type=None, metadata=None):
            self.data[key] = data

    fake = FakeStorage()

    class FakeProviders:
        storage = fake

    # ✅ Patch where the router actually calls it
    monkeypatch.setattr(
        "backend.questionnaire.sessions_router.get_providers",
        lambda: FakeProviders(),
    )

    return fake


def _legacy_session() -> Dict[str, Any]:
    return {
        "id": "legacy-1",
        "name": "Legacy Questionnaire",
        "customer": "ACME",
        "reviewer": "Reviewer",
        "status": "Draft",
        "raw_text": "Legacy questionnaire text",
        "questions": [
            {
                "id": "q1",
                "question_text": "Is data encrypted?",
                "suggested_answer": "Yes",
                "workflow_status": "In Progress",     # legacy human field
                "ai_status": "Auto Approved",         # legacy machine field
                "confidence": "0.91",                 # string -> float
                "tags": "encryption,security",        # string -> list
            }
        ],
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
    }


def test_legacy_questionnaire_session_normalizes_and_loads(fake_storage):
    client = TestClient(app)

    fake_storage.put_object(
        "stores/questionnaires.json",
        json.dumps([_legacy_session()]).encode("utf-8"),
        content_type="application/json",
    )

    resp = client.get("/questionnaires")
    assert resp.status_code == 200

    payload = resp.json()

    # Since storage is faked, only our injected session should exist
    assert len(payload) == 1
    assert payload[0]["id"] == "legacy-1"

    q = payload[0]["questions"][0]

    # 🔒 Lock the normalization forever
    assert q["review_status"] == "in_progress"
    assert q["status"] == "auto_approved"
    assert isinstance(q["confidence"], float)
    assert q["tags"] == ["encryption", "security"]
