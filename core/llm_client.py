from __future__ import annotations

import json
from typing import Any, Dict

from fastapi import HTTPException

from core.settings import get_settings
from providers.factory import get_providers

try:
    from llm_status.store import append_llm_event
except Exception:  # pragma: no cover
    append_llm_event = None  # type: ignore

try:
    from pricing.llm_pricing_store import compute_cost_usd
except Exception:  # pragma: no cover
    compute_cost_usd = None  # type: ignore


def _clip(text: str, limit: int) -> str:
    if not text:
        return ""
    return text if len(text) <= limit else text[:limit]


def _safe_json(obj: Any, limit: int) -> str:
    return _clip(json.dumps(obj, ensure_ascii=False), limit)


def _log_event(
    *,
    app: str,
    endpoint: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> None:
    if append_llm_event is None:
        return

    cost = None
    try:
        if compute_cost_usd:
            cost = compute_cost_usd(
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
    except Exception:
        pass

    try:
        append_llm_event(
            {
                "app": app,
                "endpoint": endpoint,
                "provider": "llm",
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": cost,
            }
        )
    except Exception:
        pass


async def _invoke_llm(
    *,
    system_prompt: str,
    user_payload: dict,
    temperature: float,
    request_type: str,
) -> str:
    providers = get_providers()
    settings = providers.settings

    if providers.llm is None:
        raise HTTPException(status_code=500, detail="LLM provider not configured")

    result = await providers.llm.complete(
        system_prompt=system_prompt,
        user_payload=user_payload,
        temperature=temperature,
    )

    if not result or not result.text.strip():
        raise HTTPException(
            status_code=502,
            detail=f"LLM returned empty response ({request_type})",
        )

    _log_event(
        app=request_type,
        endpoint="/analyze",
        model=settings.llm.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
    )

    return result.text.strip()


async def call_llm_for_review(req) -> str:
    payload = {
        "document_name": getattr(req, "document_name", None),
        "text": _clip(getattr(req, "text", ""), 9000),
        "hits": [h.dict() for h in getattr(req, "hits", [])],
        "prompt_override": getattr(req, "prompt_override", None),
    }

    return await _invoke_llm(
        system_prompt=(
            "You are an expert contract analyst specializing in DFARS and NIST 800-171. "
            "Be conservative. Do not hallucinate."
        ),
        user_payload=payload,
        temperature=getattr(req, "temperature", 0.2),
        request_type="contract_review",
    )


async def call_llm_question_single(question: str, similar_bank_entries: list) -> str:
    settings = get_settings()

    payload = {
        "question": _clip(question, 2000),
        "question_bank_entries": [
            {
                "id": getattr(e, "id", None),
                "text": getattr(e, "text", None),
                "answer": getattr(e, "answer", None),
                "primary_tag": getattr(e, "primary_tag", None),
                "frameworks": getattr(e, "frameworks", []),
                "status": getattr(e, "status", None),
            }
            for e in (similar_bank_entries or [])[:5]
        ],
        "instructions": "Return ONLY the answer text.",
    }

    text = await _invoke_llm(
        system_prompt="Answer conservatively using DFARS and NIST guidance.",
        user_payload=payload,
        temperature=0.2,
        request_type="questionnaire_single",
    )

    # Keep endpoint accounting consistent
    _log_event(
        app="questionnaire_single",
        endpoint="/questionnaires/analyze",
        model=settings.llm.model,
        input_tokens=0,
        output_tokens=0,
    )
    return text


async def call_llm_question_batch(questions_payload: list, knowledge_context: str) -> dict:
    settings = get_settings()

    payload = {
        "questions": questions_payload,
        "knowledge_context": _clip(knowledge_context or "", 6000),
        "instructions": (
            'Return strict JSON: {"answers": '
            '[{"id":"...","answer":"...","confidence":0.85,"inferred_tags":["CUI"]}]}'
        ),
    }

    raw = await _invoke_llm(
        system_prompt="Respond ONLY with strict JSON. No explanations.",
        user_payload=payload,
        temperature=0.2,
        request_type="questionnaire_batch",
    )

    cleaned = raw.replace("```json", "").replace("```JSON", "").replace("```", "").strip()
    try:
        data = json.loads(cleaned)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"LLM JSON parse failed: {exc}")

    answers = data.get("answers")
    if not isinstance(answers, list):
        raise HTTPException(status_code=502, detail="LLM response missing 'answers' list")

    out: Dict[str, Dict[str, Any]] = {}
    for item in answers:
        if not isinstance(item, dict):
            continue
        qid = item.get("id")
        ans = item.get("answer")
        if not qid or not isinstance(ans, str):
            continue

        try:
            conf = float(item.get("confidence", 0.6))
            conf = max(0.0, min(conf, 1.0))
        except Exception:
            conf = 0.6

        inferred_tags = item.get("inferred_tags") or []
        tags = [str(t).strip() for t in inferred_tags if t] if isinstance(inferred_tags, list) else []

        out[str(qid)] = {
            "answer": ans,
            "confidence": conf,
            "inferred_tags": tags,
        }

    _log_event(
        app="questionnaire_batch",
        endpoint="/questionnaires/analyze",
        model=settings.llm.model,
        input_tokens=0,
        output_tokens=0,
    )

    return out