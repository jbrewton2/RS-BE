from __future__ import annotations

import json
from backend.providers.factory import get_providers
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

from fastapi import APIRouter, HTTPException, Depends  # ✅ auth
from pydantic import BaseModel

from backend.questionnaire.models import QuestionnaireQuestionModel
from backend.questionnaire.bank import normalize_text  # reuse the same normalizer

# ✅ AUTH
from backend.auth.jwt import get_current_user

router = APIRouter(
    prefix="/questionnaires",
    tags=["questionnaires"],
    dependencies=[Depends(get_current_user)],  # ✅ protect all /questionnaires/*
)

BASE_DIR = Path(__file__).resolve().parent.parent
SESSIONS_FILE = BASE_DIR / "questionnaires.json"
STORE_KEY = "stores/questionnaires.json"


class QuestionnaireSessionModel(BaseModel):
    """
    Persisted questionnaire session.

    Metadata + raw_text + questions[].
    """
    id: str
    name: str
    customer: Optional[str] = ""
    reviewer: Optional[str] = ""
    date: Optional[str] = ""
    status: Optional[str] = "Draft"  # "Draft" | "In Progress" | "Finalized"

    raw_text: str
    questions: List[QuestionnaireQuestionModel]

    created_at: Optional[str] = None  # ISO8601
    updated_at: Optional[str] = None  # ISO8601


class QuestionnaireSessionUpsert(BaseModel):
    """
    Payload for creating/updating a questionnaire session.

    - If id is missing/empty → new session.
    - If id exists → update existing.
    """
    id: Optional[str] = None
    name: str
    customer: Optional[str] = ""
    reviewer: Optional[str] = ""
    date: Optional[str] = ""
    status: Optional[str] = "Draft"

    raw_text: str
    questions: List[QuestionnaireQuestionModel]


def _ensure_file_exists() -> None:


def _normalize_question_in_place(q: Dict[str, Any]) -> None:
    """
    Normalize all text fields on a single question dict.
    """
    q["question_text"] = normalize_text(q.get("question_text"))
    q["suggested_answer"] = normalize_text(q.get("suggested_answer"))
    if "tags" in q and isinstance(q["tags"], list):
        q["tags"] = [normalize_text(t) for t in q["tags"] if normalize_text(t)]


def _normalize_session_in_place(session: Dict[str, Any]) -> None:
    """
    Normalize all text fields for a single questionnaire session dict.
    """
    session["name"] = normalize_text(session.get("name"))
    session["customer"] = normalize_text(session.get("customer"))
    session["reviewer"] = normalize_text(session.get("reviewer"))
    session["date"] = normalize_text(session.get("date"))
    session["status"] = normalize_text(session.get("status") or "Draft") or "Draft"
    session["raw_text"] = normalize_text(session.get("raw_text"))

    questions = session.get("questions", [])
    if isinstance(questions, list):
        for q in questions:
            if isinstance(q, dict):
                _normalize_question_in_place(q)


def _load_sessions() -> list[dict]:
    """Load questionnaire sessions.

    Preferred: StorageProvider key STORE_KEY
    Fallback: legacy filesystem SESSIONS_FILE
    """
    # 1) StorageProvider (preferred)
    try:
        storage = get_providers().storage
        raw = storage.get_object(STORE_KEY).decode("utf-8", errors="ignore")
        data = json.loads(raw) if raw.strip() else []
        return data if isinstance(data, list) else []
    except Exception:
        pass

    # 2) Legacy filesystem fallback
    if not SESSIONS_FILE.exists():
        return []
    try:
        with SESSIONS_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []

def _save_sessions(sessions: list[dict]) -> None:
    """Persist questionnaire sessions to StorageProvider.

    Storage key: STORE_KEY
    """
    payload = json.dumps(sessions, indent=2, ensure_ascii=False).encode("utf-8", errors="ignore")

    # 1) StorageProvider (preferred)
    try:
        storage = get_providers().storage
        storage.put_object(key=STORE_KEY, data=payload, content_type="application/json", metadata=None)
        return
    except Exception:
        pass

    # 2) Legacy filesystem fallback
    SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with SESSIONS_FILE.open("w", encoding="utf-8") as f:
        json.dump(sessions, f, indent=2, ensure_ascii=False)

def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


# ---------------------------------------------------------
# GET /questionnaires
# ---------------------------------------------------------
@router.get("", response_model=List[QuestionnaireSessionModel])
def list_questionnaire_sessions() -> List[QuestionnaireSessionModel]:
    data = _load_sessions()
    return [QuestionnaireSessionModel(**item) for item in data]


# ---------------------------------------------------------
# GET /questionnaires/{session_id}
# ---------------------------------------------------------
@router.get("/{session_id}", response_model=QuestionnaireSessionModel)
def get_questionnaire_session(session_id: str) -> QuestionnaireSessionModel:
    sessions = _load_sessions()
    for item in sessions:
        if item.get("id") == session_id:
            _normalize_session_in_place(item)
            return QuestionnaireSessionModel(**item)
    raise HTTPException(status_code=404, detail="Questionnaire session not found.")


# ---------------------------------------------------------
# POST /questionnaires  (create / update)
# ---------------------------------------------------------
@router.post("", response_model=QuestionnaireSessionModel)
def upsert_questionnaire_session(payload: QuestionnaireSessionUpsert) -> QuestionnaireSessionModel:
    sessions = _load_sessions()
    now = _now_iso()

    normalized_questions: List[Dict[str, Any]] = []
    for q in payload.questions:
        q_dict = QuestionnaireQuestionModel(**q.dict()).dict()
        _normalize_question_in_place(q_dict)
        normalized_questions.append(q_dict)

    name = normalize_text(payload.name)
    customer = normalize_text(payload.customer)
    reviewer = normalize_text(payload.reviewer)
    date = normalize_text(payload.date)
    status = normalize_text(payload.status or "Draft") or "Draft"
    raw_text = normalize_text(payload.raw_text)

    # Update existing
    if payload.id:
        for idx, item in enumerate(sessions):
            if item.get("id") == payload.id:
                existing_created = item.get("created_at") or now
                updated: Dict[str, Any] = {
                    **item,
                    "id": payload.id,
                    "name": name,
                    "customer": customer,
                    "reviewer": reviewer,
                    "date": date,
                    "status": status,
                    "raw_text": raw_text,
                    "questions": normalized_questions,
                    "created_at": existing_created,
                    "updated_at": now,
                }
                _normalize_session_in_place(updated)
                sessions[idx] = updated
                _save_sessions(sessions)
                return QuestionnaireSessionModel(**updated)

        # Id provided but not found → treat as new
        session_id = payload.id
    else:
        session_id = uuid.uuid4().hex

    new_obj: Dict[str, Any] = {
        "id": session_id,
        "name": name,
        "customer": customer,
        "reviewer": reviewer,
        "date": date,
        "status": status,
        "raw_text": raw_text,
        "questions": normalized_questions,
        "created_at": now,
        "updated_at": now,
    }

    _normalize_session_in_place(new_obj)
    sessions.append(new_obj)
    _save_sessions(sessions)
    return QuestionnaireSessionModel(**new_obj)


# ---------------------------------------------------------
# DELETE /questionnaires/{session_id}
# ---------------------------------------------------------
@router.delete("/{session_id}")
def delete_questionnaire_session(session_id: str) -> Dict[str, bool]:
    sessions = _load_sessions()
    new_sessions = [s for s in sessions if s.get("id") != session_id]
    if len(new_sessions) == len(sessions):
        raise HTTPException(status_code=404, detail="Questionnaire session not found.")
    _save_sessions(new_sessions)
    return {"ok": True}

