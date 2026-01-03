from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.providers.factory import get_providers
from backend.questionnaire.models import QuestionnaireQuestionModel
from backend.questionnaire.bank import normalize_text

# ✅ Auth guard (matches the rest of CSS protected API behavior)
from backend.auth.jwt import get_current_user

router = APIRouter(
    prefix="/questionnaires",
    tags=["questionnaires"],
    dependencies=[Depends(get_current_user)],
)

BASE_DIR = Path(__file__).resolve().parent.parent
SESSIONS_FILE = BASE_DIR / "questionnaires.json"
STORE_KEY = "stores/questionnaires.json"

# ---------------------------------------------------------
# Legacy / enum coercion helpers (minimal + deterministic)
# ---------------------------------------------------------

# review_status (human workflow): Literal["needs_review", "in_progress", "complete"]
_WORKFLOW_STATUS_MAP: Dict[str, str] = {
    "needs_review": "needs_review",
    "needs review": "needs_review",
    "review": "needs_review",
    "to_review": "needs_review",
    "to review": "needs_review",
    "pending": "needs_review",
    "draft": "needs_review",
    "in_progress": "in_progress",
    "in progress": "in_progress",
    "in-progress": "in_progress",
    "progress": "in_progress",
    "working": "in_progress",
    "complete": "complete",
    "completed": "complete",
    "done": "complete",
    "final": "complete",
    "finalized": "complete",
}

# status (machine): Literal["auto_approved", "needs_review", "low_confidence", "approved"]
_QUESTION_STATUS_MAP: Dict[str, str] = {
    "auto_approved": "auto_approved",
    "auto approved": "auto_approved",
    "auto-approved": "auto_approved",
    "needs_review": "needs_review",
    "needs review": "needs_review",
    "low_confidence": "low_confidence",
    "low confidence": "low_confidence",
    "low-confidence": "low_confidence",
    "approved": "approved",
}


def _coerce_workflow_status(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = normalize_text(str(value))
    if not s:
        return None
    return _WORKFLOW_STATUS_MAP.get(s.lower())


def _coerce_question_status(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = normalize_text(str(value))
    if not s:
        return None
    return _QUESTION_STATUS_MAP.get(s.lower())


def _ensure_list_of_str(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, list):
        out: List[str] = []
        for t in value:
            st = normalize_text(t)
            if st:
                out.append(st)
        return out
    s = normalize_text(str(value))
    if not s:
        return []
    parts = [normalize_text(p) for p in s.split(",")]
    return [p for p in parts if p]


class QuestionnaireSessionModel(BaseModel):
    id: str
    name: str
    customer: Optional[str] = ""
    reviewer: Optional[str] = ""
    date: Optional[str] = ""
    status: Optional[str] = "Draft"  # "Draft" | "In Progress" | "Finalized"

    raw_text: str
    questions: List[QuestionnaireQuestionModel]

    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class QuestionnaireSessionUpsert(BaseModel):
    id: Optional[str] = None
    name: str
    customer: Optional[str] = ""
    reviewer: Optional[str] = ""
    date: Optional[str] = ""
    status: Optional[str] = "Draft"

    raw_text: str
    questions: List[QuestionnaireQuestionModel]


def _normalize_question_in_place(q: Dict[str, Any]) -> None:
    """
    Normalize a question dict in-place.

    Key goals:
    - tolerate legacy stored keys (workflow_status, ai_status)
    - enforce current model-compatible enum values
    - normalize text fields to keep determinism / consistency
    """
    if not isinstance(q, dict):
        return

    # Legacy key mapping
    if "review_status" not in q and "workflow_status" in q:
        q["review_status"] = q.get("workflow_status")
    if "status" not in q and "ai_status" in q:
        q["status"] = q.get("ai_status")

    # Normalize core text fields
    q["question_text"] = normalize_text(q.get("question_text"))
    q["suggested_answer"] = normalize_text(q.get("suggested_answer"))

    # tags (list or comma string)
    q["tags"] = _ensure_list_of_str(q.get("tags"))

    # feedback fields (optional)
    if "feedback_status" in q:
        q["feedback_status"] = normalize_text(q.get("feedback_status")) or None
    if "feedback_reason" in q:
        q["feedback_reason"] = normalize_text(q.get("feedback_reason")) or None

    # Enum coercion
    q["review_status"] = _coerce_workflow_status(q.get("review_status"))
    q["status"] = _coerce_question_status(q.get("status"))

    # Confidence coercion
    if "confidence" in q and q.get("confidence") is not None:
        try:
            q["confidence"] = float(q.get("confidence"))
        except Exception:
            q["confidence"] = None


def _normalize_session_in_place(session: Dict[str, Any]) -> None:
    if not isinstance(session, dict):
        return

    session["name"] = normalize_text(session.get("name"))
    session["customer"] = normalize_text(session.get("customer"))
    session["reviewer"] = normalize_text(session.get("reviewer"))
    session["date"] = normalize_text(session.get("date"))
    session["status"] = normalize_text(session.get("status") or "Draft") or "Draft"
    session["raw_text"] = normalize_text(session.get("raw_text"))

    qs = session.get("questions", [])
    if isinstance(qs, list):
        for q in qs:
            if isinstance(q, dict):
                _normalize_question_in_place(q)


def _load_sessions() -> List[Dict[str, Any]]:
    # StorageProvider preferred
    try:
        storage = get_providers().storage
        raw = storage.get_object(STORE_KEY).decode("utf-8", errors="ignore")
        data = json.loads(raw) if raw.strip() else []
        return data if isinstance(data, list) else []
    except Exception:
        pass

    # Legacy filesystem fallback
    if not SESSIONS_FILE.exists():
        return []
    try:
        with SESSIONS_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_sessions(sessions: List[Dict[str, Any]]) -> None:
    payload = json.dumps(sessions, indent=2, ensure_ascii=False).encode("utf-8", errors="ignore")

    # StorageProvider preferred
    try:
        storage = get_providers().storage
        storage.put_object(
            key=STORE_KEY,
            data=payload,
            content_type="application/json",
            metadata=None,
        )
        return
    except Exception:
        pass

    # Legacy filesystem fallback
    SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with SESSIONS_FILE.open("w", encoding="utf-8") as f:
        json.dump(sessions, f, indent=2, ensure_ascii=False)


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


@router.get("", response_model=List[QuestionnaireSessionModel])
def list_questionnaire_sessions() -> List[QuestionnaireSessionModel]:
    sessions = _load_sessions()

    # ✅ Normalize BEFORE Pydantic hydration to prevent list-view blowups on legacy records
    normalized: List[Dict[str, Any]] = []
    for s in sessions:
        if isinstance(s, dict):
            _normalize_session_in_place(s)
            normalized.append(s)

    return [QuestionnaireSessionModel(**s) for s in normalized]


@router.get("/{session_id}", response_model=QuestionnaireSessionModel)
def get_questionnaire_session(session_id: str) -> QuestionnaireSessionModel:
    sessions = _load_sessions()
    for s in sessions:
        if s.get("id") == session_id:
            _normalize_session_in_place(s)
            return QuestionnaireSessionModel(**s)
    raise HTTPException(status_code=404, detail="Questionnaire session not found.")


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

    if payload.id:
        for idx, s in enumerate(sessions):
            if s.get("id") == payload.id:
                created_at = s.get("created_at") or now
                updated = {
                    **s,
                    "id": payload.id,
                    "name": name,
                    "customer": customer,
                    "reviewer": reviewer,
                    "date": date,
                    "status": status,
                    "raw_text": raw_text,
                    "questions": normalized_questions,
                    "created_at": created_at,
                    "updated_at": now,
                }
                _normalize_session_in_place(updated)
                sessions[idx] = updated
                _save_sessions(sessions)
                return QuestionnaireSessionModel(**updated)
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


@router.delete("/{session_id}")
def delete_questionnaire_session(session_id: str) -> Dict[str, bool]:
    sessions = _load_sessions()
    new_sessions = [s for s in sessions if s.get("id") != session_id]
    if len(new_sessions) == len(sessions):
        raise HTTPException(status_code=404, detail="Questionnaire session not found.")
    _save_sessions(new_sessions)
    return {"ok": True}
