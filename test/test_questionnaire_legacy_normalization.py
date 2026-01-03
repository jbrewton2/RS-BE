import json
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from main import app
from auth.jwt import get_current_user


@pytest.fixture(autouse=True)
def override_auth():
    app.dependency_overrides[get_current_user] = lambda: {"sub": "test-user"}
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture(autouse=True)
def fake_storage(monkeypatch):
    class FakeStorage:
        def __init__(self):
            self.data = {}

        def get_object(self, key: str) -> bytes:
            return self.data.get(key, b"[]")

        def put_object(self, key: str, data: bytes, content_type=None, metadata=None):
            self.data[key] = data

        def head_object(self, key: str):
            if key not in self.data:
                raise FileNotFoundError(key)

        def delete_object(self, key: str) -> None:
            self.data.pop(key, None)

    fake = FakeStorage()

    class FakeProviders:
        storage = fake

    monkeypatch.setattr("questionnaire.sessions_router.get_providers", lambda: FakeProviders())
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
                "workflow_status": "In Progress",
                "ai_status": "Auto Approved",
                "confidence": "0.91",
                "tags": "encryption,security",
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

    assert len(payload) == 1
    assert payload[0]["id"] == "legacy-1"

    q = payload[0]["questions"][0]
    assert q["review_status"] == "in_progress"
    assert q["status"] == "auto_approved"
    assert isinstance(q["confidence"], float)
    assert q["tags"] == ["encryption", "security"]
