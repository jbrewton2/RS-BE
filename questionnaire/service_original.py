# backend/app/questionnaire/service.py
from __future__ import annotations
from typing import List, Optional

from fastapi import HTTPException
from core.providers import providers_from_request

from .models import (
    QuestionnaireQuestionModel,
    QuestionBankEntryModel,
    QuestionnaireAnalyzeRequest,
    AnalyzeQuestionnaireResponse,
)
from .parser import parse_questions_from_text
from .bank import load_question_bank
from .scoring import derive_status_and_confidence
from ..core.llm_client import call_chat_llm
from ..core.config import DEFAULT_LLM_MODEL
from main import QUESTIONNAIRE_SYSTEM_PROMPT  # reuse your system prompt

def _question_similarity(a: str, b: str) -> float:
    a_tokens = set(a.lower().split())
    b_tokens = set(b.lower().split())
    if not a_tokens or not b_tokens:
        return 0.0
    overlap = len(a_tokens & b_tokens)
    return overlap / max(1, len(a_tokens))

async def analyze_questionnaire(body: QuestionnaireAnalyzeRequest) -> AnalyzeQuestionnaireResponse:
    text = (body.raw_text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="raw_text must be non-empty.")

    questions = parse_questions_from_text(text)
    if not questions:
        return AnalyzeQuestionnaireResponse(raw_text=text, questions=[], overall_confidence=None)

    bank_entries = load_question_bank(storage)
    BANK_STRONG_THRESHOLD = 0.7
    BANK_WEAK_THRESHOLD = 0.4

    for q in questions:
        best_entry: Optional[QuestionBankEntryModel] = None
        best_score = 0.0

        for entry in bank_entries:
            s = _question_similarity(q.question_text, entry.text)
            if s > best_score:
                best_score = s
                best_entry = entry

        # Strong match → bank answer
        if best_entry and best_score >= BANK_STRONG_THRESHOLD:
            q.suggested_answer = best_entry.answer
            q.answer_source = "bank"
            q.matched_bank_id = best_entry.id
            q.confidence = min(0.98, 0.8 + (best_score - BANK_STRONG_THRESHOLD) * 0.4)
            continue

        # LLM disabled → no answer
        if not body.llm_enabled:
            q.suggested_answer = None
            q.answer_source = None
            q.matched_bank_id = best_entry.id if best_entry else None
            q.confidence = None
            continue

        # Weak match → pass context entries to LLM
        similar_context: List[QuestionBankEntryModel] = []
        if best_entry and best_score >= BANK_WEAK_THRESHOLD:
            similar_context.append(best_entry)

        user_payload = {
            "question": q.question_text,
            "question_bank_entries": [
                {
                    "id": e.id,
                    "text": e.text,
                    "answer": e.answer,
                    "primary_tag": e.primary_tag,
                    "frameworks": e.frameworks or [],
                    "status": e.status,
                }
                for e in similar_context
            ],
        }

        try:
            answer = await call_chat_llm(
                system_prompt=QUESTIONNAIRE_SYSTEM_PROMPT,
                user_payload=user_payload,
                request_type="questionnaire",
                model_override=DEFAULT_LLM_MODEL,
            )
            if answer.strip():
                q.suggested_answer = answer.strip()
                q.answer_source = "llm"
                q.matched_bank_id = best_entry.id if best_entry else None
                q.confidence = 0.6
            else:
                q.suggested_answer = None
                q.answer_source = None
                q.matched_bank_id = best_entry.id if best_entry else None
                q.confidence = None
        except HTTPException as exc:
            print(f"Questionnaire LLM error for '{q.id}': {exc.detail}")
            q.suggested_answer = None
            q.answer_source = None
            q.matched_bank_id = best_entry.id if best_entry else None
            q.confidence = None

    overall_conf = derive_status_and_confidence(questions)

    return AnalyzeQuestionnaireResponse(
        raw_text=text,
        questions=questions,
        overall_confidence=overall_conf if overall_conf > 0 else None,
    )


