from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import HTTPException

from core.config import ORG_POSTURE_SUMMARY, KNOWLEDGE_DOCS_DIR
from core.settings import get_settings

try:
    from llm_status.store import append_llm_event
except Exception:  # pragma: no cover
    append_llm_event = None  # type: ignore

try:
    from pricing.llm_pricing_store import compute_cost_usd
except Exception:  # pragma: no cover
    compute_cost_usd = None  # type: ignore


def _is_chat_endpoint(url: str) -> bool:
    return url.rstrip("/").endswith("/api/chat")


def _clip_text(text: str, max_chars: int) -> str:
    if not text:
        return ""
    s = str(text)
    return s if len(s) <= max_chars else s[:max_chars]


def _safe_json_dumps(obj: Any, max_chars: int) -> str:
    s = json.dumps(obj, ensure_ascii=False, indent=None)
    return _clip_text(s, max_chars)


def _build_httpx_timeout(total_timeout: float, connect_timeout: float):
    # Works across httpx versions
    try:
        return httpx.Timeout(
            timeout=total_timeout,
            connect=connect_timeout,
            read=total_timeout,
            write=connect_timeout,
            pool=connect_timeout,
        )
    except TypeError:
        return total_timeout


def _log_llm_event(
    *,
    app: str,
    endpoint: str,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> None:
    if append_llm_event is None:
        return

    cost_usd = None
    try:
        if compute_cost_usd is not None:
            cost_usd = compute_cost_usd(model=model, input_tokens=input_tokens, output_tokens=output_tokens)
    except Exception:
        cost_usd = None

    try:
        append_llm_event(
            event={
                "app": app,
                "endpoint": endpoint,
                "provider": provider,
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": cost_usd,
            }
        )
    except Exception:
        pass


def _format_http_status_error(exc: httpx.HTTPStatusError) -> str:
    status = None
    body_snip = ""
    try:
        status = exc.response.status_code
    except Exception:
        status = None

    try:
        body_snip = _clip_text(exc.response.text, 600)
    except Exception:
        body_snip = ""

    if status is not None and body_snip:
        return f"HTTP {status}: {body_snip}"
    if status is not None:
        return f"HTTP {status}"
    return repr(exc)


async def _llm_post(payload: Dict[str, Any], request_type: str) -> Tuple[str, int, int]:
    s = get_settings().llm
    url = s.api_url
    model = s.model

    timeout = _build_httpx_timeout(s.timeout_seconds, s.connect_timeout_seconds)
    last_detail: str = ""

    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(1, s.max_attempts + 1):
            try:
                r = await client.post(url, json=payload)
                r.raise_for_status()
                data = r.json()

                if _is_chat_endpoint(url):
                    content = (data.get("message") or {}).get("content") or ""
                    input_tokens = int((data.get("usage") or {}).get("prompt_tokens") or 0)
                    output_tokens = int((data.get("usage") or {}).get("completion_tokens") or 0)
                else:
                    content = data.get("response") or ""
                    input_tokens = int(data.get("prompt_eval_count") or 0)
                    output_tokens = int(data.get("eval_count") or 0)

                if not isinstance(content, str) or not content.strip():
                    raise HTTPException(status_code=502, detail=f"LLM returned empty content ({request_type})")

                return content.strip(), input_tokens, output_tokens

            except httpx.HTTPStatusError as exc:
                last_detail = _format_http_status_error(exc)
            except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError) as exc:
                last_detail = f"{type(exc).__name__}: {str(exc)}"
            except HTTPException:
                raise
            except Exception as exc:
                last_detail = f"{type(exc).__name__}: {str(exc)}"

            if attempt < s.max_attempts:
                backoff = s.backoff_seconds[min(attempt - 1, len(s.backoff_seconds) - 1)]
                await asyncio.sleep(backoff)
                continue
            break

    if last_detail:
        raise HTTPException(status_code=502, detail=f"LLM request ({request_type}) failed: {last_detail}")
    raise HTTPException(status_code=502, detail=f"LLM request ({request_type}) failed")


async def _load_knowledge_context(knowledge_doc_ids: Optional[List[str]]) -> str:
    if not knowledge_doc_ids:
        return ""
    chunks: List[str] = []
    for kid in knowledge_doc_ids:
        p = Path(KNOWLEDGE_DOCS_DIR) / f"{kid}.txt"
        if p.exists():
            try:
                chunks.append(p.read_text(encoding="utf-8", errors="ignore")[:4000])
            except Exception:
                pass
    return "\n\n".join(chunks).strip()


async def call_llm_for_review(req) -> str:
    s = get_settings().llm
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

    if _is_chat_endpoint(s.api_url):
        payload = {
            "model": s.model,
            "messages": [
                {"role": "system", "content": _clip_text(system_prompt, 7000)},
                {"role": "user", "content": _safe_json_dumps(user_payload, 9000)},
            ],
            "temperature": getattr(req, "temperature", None) or 0.2,
            "stream": False,
        }
    else:
        payload = {
            "model": s.model,
            "prompt": f"{_clip_text(system_prompt, 7000)}\n\nUSER_PAYLOAD_JSON:\n{_safe_json_dumps(user_payload, 9000)}\n",
            "temperature": getattr(req, "temperature", None) or 0.2,
            "stream": False,
        }

    content, input_tokens, output_tokens = await _llm_post(payload, "contract-review")
    _log_llm_event(
        app="contract_review",
        endpoint="/analyze",
        provider=s.provider,
        model=s.model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    return content


async def call_llm_question_single(question: str, similar_bank_entries: list) -> str:
    s = get_settings().llm

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

    if _is_chat_endpoint(s.api_url):
        payload = {
            "model": s.model,
            "messages": [
                {"role": "system", "content": "Answer conservatively using NIST/DFARS guidance. Return ONLY answer text."},
                {"role": "user", "content": _safe_json_dumps(user_payload, 7000)},
            ],
            "temperature": 0.2,
            "stream": False,
        }
    else:
        payload = {
            "model": s.model,
            "prompt": f"Answer conservatively using NIST/DFARS guidance. Return ONLY answer text.\n\n{_safe_json_dumps(user_payload, 7000)}",
            "temperature": 0.2,
            "stream": False,
        }

    content, input_tokens, output_tokens = await _llm_post(payload, "questionnaire-single")
    _log_llm_event(
        app="questionnaire_single",
        endpoint="/questionnaires/analyze",
        provider=s.provider,
        model=s.model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    return content


async def call_llm_question_batch(questions_payload: list, knowledge_context: str) -> dict:
    s = get_settings().llm

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

    if _is_chat_endpoint(s.api_url):
        payload = {
            "model": s.model,
            "messages": [
                {"role": "system", "content": "Respond ONLY with strict JSON. No explanations."},
                {"role": "user", "content": _safe_json_dumps(user_payload, 9000)},
            ],
            "temperature": 0.2,
            "stream": False,
        }
    else:
        payload = {
            "model": s.model,
            "prompt": f"Respond ONLY with strict JSON. No explanations.\n\n{_safe_json_dumps(user_payload, 9000)}",
            "temperature": 0.2,
            "stream": False,
        }

    raw, input_tokens, output_tokens = await _llm_post(payload, "questionnaire-batch")

    cleaned = raw.replace("```json", "").replace("```JSON", "").replace("```", "").strip()
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
        provider=s.provider,
        model=s.model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    return out