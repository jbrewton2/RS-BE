from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Depends  # ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã¢â‚¬Â¦ÃƒÂ¢Ã¢â€šÂ¬Ã…â€œÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â¦ auth, Request
from core.deps import StorageDep
from core.providers import providers_from_request

from questionnaire.models import (
    QuestionnaireAnalyzeRequest,
    AnalyzeQuestionnaireResponse,
    QuestionnaireFeedbackRequest,
    QuestionBankEntryModel,
    QuestionBankUpsertModel,
)
from questionnaire.service import analyze_questionnaire
from questionnaire.bank import (
    load_question_bank,
    save_question_bank,
    normalize_text,
)
from questionnaire.generator import generate_question_variants

# ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã¢â‚¬Â¦ÃƒÂ¢Ã¢â€šÂ¬Ã…â€œÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â¦ AUTH
from auth.jwt import get_current_user

# ---------------------------------------------------------------------
# Routers (AUTH ENFORCED HERE)
# ---------------------------------------------------------------------

router = APIRouter(
    prefix="/questionnaire",
    tags=["questionnaire"],
    dependencies=[Depends(get_current_user)],  # ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã¢â‚¬Â¦ÃƒÂ¢Ã¢â€šÂ¬Ã…â€œÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â¦ protect all /questionnaire/*
)

question_bank_router = APIRouter(
    tags=["question-bank"],
    dependencies=[Depends(get_current_user)],  # ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã¢â‚¬Â¦ÃƒÂ¢Ã¢â€šÂ¬Ã…â€œÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â¦ protect /question-bank/* routes too
)

# ---------------------------------------------------------------------
# /questionnaire/analyze
# ---------------------------------------------------------------------


@router.post("/analyze", response_model=AnalyzeQuestionnaireResponse)
async def questionnaire_analyze_route(body: QuestionnaireAnalyzeRequest, storage: StorageDep):
    """
    Analyze a questionnaire body.

    This is the main entrypoint the frontend calls when you hit
    "Analyze" on the questionnaire page.
    """
    raw_len = len((body.raw_text or "").strip())
    llm_enabled = getattr(body, "llm_enabled", True)
    knowledge_doc_ids = getattr(body, "knowledge_doc_ids", None) or []

    print(
        "[QUESTIONNAIRE] /questionnaire/analyze route: start "
        f"raw_len={raw_len}, llm_enabled={llm_enabled}, "
        f"knowledge_doc_ids={knowledge_doc_ids}"
    )

    resp = await analyze_questionnaire(body, storage)
    print(
        "[QUESTIONNAIRE] /questionnaire/analyze route: done "
        f"returning {len(resp.questions)} questions"
    )

    return resp


# ---------------------------------------------------------------------
# Internal helper to upsert a bank entry
# ---------------------------------------------------------------------


def _upsert_bank_entry(payload: QuestionBankUpsertModel, storage: StorageDep) -> QuestionBankEntryModel:
    """
    Add or update a QuestionBankEntryModel.

    - If payload.id matches an existing entry -> update fields (including variants).
    - Otherwise -> create a new entry with a generated id.

    All text fields are normalized before saving.
    """
    bank = load_question_bank(storage)

    # Normalize incoming payload fields
    text = normalize_text(payload.text or "")
    answer = normalize_text(payload.answer or "")
    primary_tag = normalize_text(payload.primaryTag or "") or None
    frameworks = [
        normalize_text(f)
        for f in (payload.frameworks or [])
        if normalize_text(f)
    ]
    status = normalize_text(payload.status or "") or (payload.status or "approved")
    variants = [
        normalize_text(v)
        for v in (payload.variants or [])
        if normalize_text(v)
    ]

    # Update existing
    if payload.id:
        for idx, existing in enumerate(bank):
            if existing.id == payload.id:
                updated = existing.copy(
                    update={
                        "text": text,
                        "answer": answer,
                        "primary_tag": primary_tag,
                        "frameworks": frameworks,
                        "status": status or existing.status,
                        "variants": variants or existing.variants,
                    }
                )
                bank[idx] = updated
                save_question_bank(storage, bank)
                return updated

    # Create new
    new_id = payload.id or f"bank-{len(bank) + 1}"
    new_entry = QuestionBankEntryModel(
        id=new_id,
        text=text,
        answer=answer,
        primary_tag=primary_tag,
        frameworks=frameworks,
        status=status or "approved",
        variants=variants,
    )
    bank.append(new_entry)
    save_question_bank(storage, bank)
    return new_entry


# ---------------------------------------------------------------------
# /questionnaire/feedback
# ---------------------------------------------------------------------


@router.post("/feedback")
async def questionnaire_feedback(payload: QuestionnaireFeedbackRequest, storage=StorageDep):
    """
    Record approval / rejection feedback for a single questionnaire answer.

    All text fields are normalized before being written into the bank.
    """
    bank = load_question_bank(storage)
    updated_entry: Optional[QuestionBankEntryModel] = None

    # Locate existing bank entry if we have a matched_bank_id
    existing: Optional[QuestionBankEntryModel] = None
    if payload.matched_bank_id:
        for e in bank:
            if e.id == payload.matched_bank_id:
                existing = e
                break

    now_iso = datetime.utcnow().isoformat() + "Z"

    # -------------------------
    # Handle REJECTION feedback
    # -------------------------
    if not payload.approved:
        if existing and payload.feedback_reason:
            reason = normalize_text(payload.feedback_reason)
            if reason:
                existing.rejection_reasons.append(reason)
                existing.last_feedback = reason
                save_question_bank(storage, bank)
        return {"ok": True, "updated_bank_entry": None}

    # ------------------------------------------
    # APPROVED ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â but user did NOT choose promote
    # ------------------------------------------
    if payload.approved and not payload.promote_to_bank:
        if existing:
            existing.usage_count += 1
            existing.last_used_at = now_iso
            save_question_bank(storage, bank)
        return {"ok": True, "updated_bank_entry": None}

    # ------------------------------------------
    # APPROVED + PROMOTE TO BANK (seed/update)
    # ------------------------------------------
    final_ans = normalize_text(payload.final_answer or "")
    if not final_ans:
        raise HTTPException(
            status_code=400,
            detail=(
                "final_answer is required when promote_to_bank "
                "is true and approved."
            ),
        )

    question_text_raw = (payload.question_text or "").strip()
    question_text = normalize_text(question_text_raw) or f"Question {payload.question_id}"

    if existing:
        # Update existing entry
        existing.text = question_text
        existing.answer = final_ans
        existing.status = "approved"
        existing.usage_count += 1
        existing.last_used_at = now_iso
        updated_entry = existing
    else:
        # Create new entry
        new_id = payload.matched_bank_id or f"bank-{len(bank) + 1}"
        new_entry = QuestionBankEntryModel(
            id=new_id,
            text=question_text,
            answer=final_ans,
            status="approved",
            primary_tag=None,
            frameworks=[],
            rejection_reasons=[],
            last_feedback=None,
            usage_count=1,
            last_used_at=now_iso,
        )
        bank.append(new_entry)
        updated_entry = new_entry

    # Auto-generate paraphrase variants for better matching
    if updated_entry is not None:
        try:
            variants = await generate_question_variants(updated_entry.text)
            if variants:
                cleaned_variants = [
                    normalize_text(v) for v in variants if normalize_text(v)
                ]
                existing_variants = getattr(updated_entry, "variants", [])
                merged = list(dict.fromkeys(existing_variants + cleaned_variants))
                updated_entry.variants = merged
        except Exception as exc:
            # Do not fail feedback endpoint if variant generation fails
            print("[QUESTIONNAIRE] Failed to generate variants:", exc)

    save_question_bank(storage, bank)
    return {"ok": True, "updated_bank_entry": updated_entry}


# ---------------------------------------------------------------------
# /questionnaire/bank (for current frontend)
# ---------------------------------------------------------------------


@router.get("/bank", response_model=List[QuestionBankEntryModel])
async def get_questionnaire_bank_route(storage=StorageDep):
    return load_question_bank(storage)


@router.post("/bank", response_model=QuestionBankEntryModel)
async def upsert_questionnaire_bank_route(entry: QuestionBankUpsertModel, storage=StorageDep):
    return _upsert_bank_entry(entry, storage)
# ---------------------------------------------------------------------
# Top-level /question-bank endpoints (for older callers)
# ---------------------------------------------------------------------


@question_bank_router.get("/question-bank", response_model=List[QuestionBankEntryModel])
async def get_question_bank_route(storage=StorageDep):
    return load_question_bank(storage)


@question_bank_router.post("/question-bank", response_model=QuestionBankEntryModel)
async def upsert_question_bank_route(entry: QuestionBankUpsertModel, storage=StorageDep):
    return _upsert_bank_entry(entry, storage)
@question_bank_router.delete("/question-bank/{entry_id}")
async def delete_question_bank_entry_route(entry_id: str, storage: StorageDep):
    bank = load_question_bank(storage)
    new_bank = [b for b in bank if b.id != entry_id]
    if len(new_bank) == len(bank):
        raise HTTPException(status_code=404, detail="Bank entry not found")
    save_question_bank(storage, new_bank)
    return {"ok": True}





