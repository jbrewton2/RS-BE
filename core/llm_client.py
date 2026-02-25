from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import HTTPException

from core.settings import get_settings

# These are org/app constants you already maintain in core.config
from core.config import ORG_POSTURE_SUMMARY, KNOWLEDGE_DOCS_DIR

# Optional instrumentation hooks (safe if module not present)
append_llm_event = None  # llm_status removed
try:
    from pricing.llm_pricing_store import compute_cost_usd
except Exception:  # pragma: no cover
    compute_cost_usd = None  # type: ignore


# ---------------------------------------------------------------------------
# Small utils
# ---------------------------------------------------------------------------
def _clip_text(*args, **kwargs):
    return

def _provider_tag() -> str:
    # Used only for logging. Do not tie semantics to it.
    s = get_settings()
    return (s.llm.provider or "unknown").strip().lower()


def _build_chat_payload(model: str, system: str, user: str, temperature: float) -> Dict[str, Any]:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": _clip_text(system, 7000)},
            {"role": "user", "content": _clip_text(user, 9000)},
        ],
        "temperature": temperature,
        "stream": False,
    }


def _build_generate_payload(model: str, prompt: str, temperature: float) -> Dict[str, Any]:
    return {
        "model": model,
        "prompt": _clip_text(prompt, 14000),
        "temperature": temperature,
        "stream": False,
    }


# ---------------------------------------------------------------------------
# Public API used by routes/services
# ---------------------------------------------------------------------------
async def call_llm_for_review(req) -> str:
    """
    Used by /analyze.
    """
    s = get_settings()
    model = s.llm.model
    url = s.llm.api_url

    knowledge_context = await _load_knowledge_context(getattr(req, "knowledge_doc_ids", None))

    user_payload = {
        "document_name": getattr(req, "document_name", None),
        "text": _clip_text(getattr(req, "text", ""), 9000),
        "hits": [h.dict() for h in getattr(req, "hits", [])],
        "knowledge_context": _clip_text(knowledge_context, 6000),
        "prompt_override": getattr(req, "prompt_override", None),
    }

    system_prompt = (
        "You are an expert contract analyst specializing in DFARS, NIST 800-171, and risk identification. "
        "Be conservative and do not hallucinate."
    )

    temp = float(getattr(req, "temperature", None) or 0.2)

    if _is_chat_endpoint(url):
        payload = _build_chat_payload(
            model=model,
            system=system_prompt,
            user=_safe_json_dumps(user_payload, 9000),
            temperature=temp,
        )
    else:
        payload = _build_generate_payload(
            model=model,
            prompt=f"{system_prompt}\n\nUSER_PAYLOAD_JSON:\n{_safe_json_dumps(user_payload, 9000)}\n",
            temperature=temp,
        )

    content, input_tokens, output_tokens = await _llm_http_post(payload, "contract-review")

    _log_llm_event(
        app="contract_review",
        endpoint="/analyze",
        provider=_provider_tag(),
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    return content


async def call_llm_question_single(question: str, similar_bank_entries: list) -> str:
    """
    Used by questionnaire generation flow.
    """
    s = get_settings()
    model = s.llm.model
    url = s.llm.api_url

    bank_items = [
        {
            "id": getattr(e, "id", None),
            "text": getattr(e, "text", None),
            "answer": getattr(e, "answer", None),
            "primary_tag": getattr(e, "primary_tag", None),
            "frameworks": getattr(e, "frameworks", None) or [],
            "status": getattr(e, "status", None),
            "rejection_reasons": getattr(e, "rejection_reasons", None) or [],
        }
        for e in (similar_bank_entries or [])[:5]
    ]

    user_payload = {
        "question": _clip_text(question, 2000),
        "question_bank_entries": bank_items,
        "org_posture": _clip_text(ORG_POSTURE_SUMMARY, 4000),
        "instructions": "Return ONLY the answer text (no JSON, no explanation).",
    }

    system_prompt = "Answer conservatively using NIST/DFARS guidance. Return ONLY answer text."
    temp = 0.2

    if _is_chat_endpoint(url):
        payload = _build_chat_payload(
            model=model,
            system=system_prompt,
            user=_safe_json_dumps(user_payload, 7000),
            temperature=temp,
        )
    else:
        payload = _build_generate_payload(
            model=model,
            prompt=f"{system_prompt}\n\n{_safe_json_dumps(user_payload, 7000)}",
            temperature=temp,
        )

    content, input_tokens, output_tokens = await _llm_http_post(payload, "questionnaire-single")

    _log_llm_event(
        app="questionnaire_single",
        endpoint="/questionnaires/analyze",
        provider=_provider_tag(),
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    return content


async def call_llm_question_batch(questions_payload: list, knowledge_context: str) -> dict:
    """
    Batch answering. Returns { "<id>": {answer, confidence, inferred_tags} }
    """
    s = get_settings()
    model = s.llm.model
    url = s.llm.api_url

    user_payload = {
        "questions": questions_payload,
        "knowledge_context": _clip_text(knowledge_context or "", 6000),
        "org_posture": _clip_text(ORG_POSTURE_SUMMARY, 4000),
        "instructions": (
            'Return strict JSON: {"answers": '
            '[{"id":"...","answer":"...","confidence":0.85,"inferred_tags":["CUI"]}]}. '
            "No text outside JSON."
        ),
    }

    system_prompt = "Respond ONLY with strict JSON. No explanations."
    temp = 0.2

    if _is_chat_endpoint(url):
        payload = _build_chat_payload(
            model=model,
            system=system_prompt,
            user=_safe_json_dumps(user_payload, 9000),
            temperature=temp,
        )
    else:
        payload = _build_generate_payload(
            model=model,
            prompt=f"{system_prompt}\n\n{_safe_json_dumps(user_payload, 9000)}",
            temperature=temp,
        )

    raw, input_tokens, output_tokens = await _llm_http_post(payload, "questionnaire-batch")

    cleaned = (
        raw.replace("```json", "")
           .replace("```JSON", "")
           .replace("```", "")
           .strip()
    )

    try:
        data = json.loads(cleaned)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Batch LLM JSON parse failed: {exc}. Raw: {raw[:200]}")

    answers = data.get("answers")
    if not isinstance(answers, list):
        raise HTTPException(status_code=502, detail="Batch LLM response missing 'answers' list")

    out: Dict[str, Dict[str, Any]] = {}
    for item in answers:
        if not isinstance(item, dict):
            continue
        qid = item.get("id")
        ans = item.get("answer")
        if not qid or not isinstance(ans, str):
            continue

        conf = item.get("confidence", 0.6)
        try:
            conf_val = float(conf)
            conf_val = max(0.0, min(conf_val, 1.0))
        except Exception:
            conf_val = 0.6

        inferred_tags = item.get("inferred_tags")
        tags = [str(t).strip() for t in inferred_tags if t] if isinstance(inferred_tags, list) else []

        out[str(qid)] = {
            "answer": ans,
            "confidence": conf_val,
            "inferred_tags": tags,
        }

    _log_llm_event(
        app="questionnaire_batch",
        endpoint="/questionnaires/analyze",
        provider=_provider_tag(),
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    return out
