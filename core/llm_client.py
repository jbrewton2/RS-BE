from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Tuple, List, Optional, Any, Dict

import httpx
from fastapi import HTTPException

from backend.core.config import (
    OLLAMA_API_URL as CONFIG_OLLAMA_API_URL,
    DEFAULT_LLM_MODEL as CONFIG_DEFAULT_LLM_MODEL,
    SYSTEM_PROMPT_BASE,
    QUESTIONNAIRE_SYSTEM_PROMPT,
    KNOWLEDGE_DOCS_DIR,
    REVIEW_SYSTEM_PROMPT,
    ORG_POSTURE_SUMMARY,
)
from backend.llm_status.store import append_llm_event
from backend.pricing.llm_pricing_store import compute_cost_usd
from backend.llm_config_store import load_llm_config, LLMConfig


# ===================================================================
# ENV OVERRIDES (deployment-friendly)
# ===================================================================

def _get_ollama_api_url() -> str:
    return (os.getenv("OLLAMA_API_URL") or CONFIG_OLLAMA_API_URL or "").strip()


def _get_default_model() -> str:
    return (os.getenv("OLLAMA_MODEL") or CONFIG_DEFAULT_LLM_MODEL or "").strip()


def _get_num_ctx() -> int:
    """
    Ollama context window control (tokens). Default 4096 based on your logs.
    You can override with OLLAMA_NUM_CTX=8192, etc (if your Ollama runner supports it).
    """
    raw = (os.getenv("OLLAMA_NUM_CTX") or "").strip()
    if not raw:
        return 4096
    try:
        v = int(raw)
        return max(1024, min(v, 131072))
    except Exception:
        return 4096


def _is_chat_endpoint(url: str) -> bool:
    return url.rstrip("/").endswith("/api/chat")


# ===================================================================
# LLM CONFIG HELPERS (org posture + prompt override)
# ===================================================================

def _get_effective_llm_config() -> Optional[LLMConfig]:
    try:
        return load_llm_config()
    except Exception:
        return None


def _get_effective_org_posture() -> str:
    cfg = _get_effective_llm_config()
    if cfg and cfg.org_posture:
        text = cfg.org_posture.strip()
        if text:
            return text
    return ORG_POSTURE_SUMMARY


def _apply_prompt_override(base_prompt: str) -> str:
    cfg = _get_effective_llm_config()
    extra = (cfg.prompt_override or "").strip() if cfg else ""
    if not extra:
        return base_prompt
    return base_prompt + "\n\nADDITIONAL INSTRUCTIONS:\n" + extra


def _build_review_system_prompt(prompt_override: Optional[str]) -> str:
    if prompt_override:
        return prompt_override.strip()
    base = REVIEW_SYSTEM_PROMPT or SYSTEM_PROMPT_BASE
    return _apply_prompt_override(base)


# ===================================================================
# TOKEN COST + LOGGING
# ===================================================================

def _compute_cost_for_model(model: str, input_tokens: int, output_tokens: int) -> Tuple[float, float, float]:
    total_cost = compute_cost_usd(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    return (0.0, 0.0, total_cost)


def _log_llm_event(
    *,
    app: str,
    endpoint: str,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> None:
    try:
        _, _, total_cost = _compute_cost_for_model(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        append_llm_event(
            {
                "app": app,
                "endpoint": endpoint,
                "provider": provider,
                "model": model,
                "input_tokens": int(input_tokens),
                "output_tokens": int(output_tokens),
                "input_cost": 0.0,
                "output_cost": 0.0,
                "total_cost": float(total_cost),
            }
        )
    except Exception:
        pass


# ===================================================================
# PROMPT / PAYLOAD SIZE DEFENSES
# ===================================================================

def _clip_text(value: Any, max_chars: int) -> str:
    """
    Defensive clipping so we don't send 70k+ character prompts and get
    hard-truncated at the Ollama runner (your logs show truncation + failures).
    """
    t = (value or "").toString() if hasattr(value, "toString") else str(value or "")
    t = t.strip()
    if len(t) <= max_chars:
        return t
    return t[: max(0, max_chars - 1)] + "â€¦"


def _safe_json_dumps(obj: Any, max_chars: int) -> str:
    s = json.dumps(obj, ensure_ascii=False)
    if len(s) <= max_chars:
        return s
    return s[: max(0, max_chars - 1)] + "â€¦"


# ===================================================================
# INTERNAL OLLAMA POST (reliable)
# ===================================================================

async def _ollama_post(payload: dict, request_type: str) -> Tuple[str, int, int]:
    """
    Call Ollama and return:
      - assistant content string
      - prompt_eval_count as input_tokens
      - eval_count as output_tokens

    Reliability:
      - explicit timeouts (avoid ~15s disconnect behavior)
      - retries + backoff
      - richer error detail
    """
    url = _get_ollama_api_url()
    if not url:
        raise HTTPException(
            status_code=500,
            detail="OLLAMA_API_URL is not configured (env OLLAMA_API_URL or core/config.py).",
        )

    # Hardening: ensure num_ctx is present for both chat/generate where supported
    num_ctx = _get_num_ctx()
    payload.setdefault("options", {})
    if isinstance(payload.get("options"), dict):
        payload["options"].setdefault("num_ctx", num_ctx)

    # Explicit timeouts (connect can be slow; inference can be minutes)
    timeout = httpx.Timeout(connect=30.0, read=600.0, write=600.0, pool=600.0)

    max_attempts = 3
    backoff = [1.0, 2.0, 4.0]
    last_exc: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, json=payload)

            if resp.status_code >= 400:
                body_preview = (resp.text or "").strip().replace("\n", " ")[:240]
                raise httpx.HTTPStatusError(
                    f"HTTP {resp.status_code} from Ollama: {body_preview}",
                    request=resp.request,
                    response=resp,
                )

            raw = (resp.text or "").strip()

            # JSONL fallback
            try:
                data = resp.json()
            except Exception:
                lines = [ln for ln in raw.splitlines() if ln.strip()]
                if not lines:
                    raise HTTPException(
                        status_code=502,
                        detail=f"LLM returned empty body ({request_type})",
                    )
                try:
                    data = json.loads(lines[-1])
                except Exception as exc:
                    raise HTTPException(
                        status_code=502,
                        detail=f"LLM JSON parse failed ({request_type}): {exc}",
                    )

            # Extract assistant content (chat vs generate)
            content = None
            if isinstance(data, dict):
                msg = data.get("message")
                if isinstance(msg, dict):
                    content = msg.get("content")
                else:
                    content = data.get("response")

            if not isinstance(content, str) or not content.strip():
                raise HTTPException(
                    status_code=502,
                    detail=f"LLM returned empty content ({request_type})",
                )

            # Token counts
            try:
                input_tokens = int(data.get("prompt_eval_count") or 0)
            except Exception:
                input_tokens = 0

            try:
                output_tokens = int(data.get("eval_count") or 0)
            except Exception:
                output_tokens = 0

            return content.strip(), input_tokens, output_tokens

        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            if attempt < max_attempts:
                await asyncio.sleep(backoff[attempt - 1])
                continue
            break
        except HTTPException:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts:
                await asyncio.sleep(backoff[attempt - 1])
                continue
            break

    raise HTTPException(
        status_code=502,
        detail=f"LLM request ({request_type}) failed after {max_attempts} attempts: {last_exc}",
    )


# ===================================================================
# REVIEW ANALYSIS (contract review)
# ===================================================================

async def _load_knowledge_context(knowledge_doc_ids: Optional[List[str]]) -> str:
    if not knowledge_doc_ids:
        return ""

    chunks: List[str] = []
    for kid in knowledge_doc_ids:
        p = Path(KNOWLEDGE_DOCS_DIR) / f"{kid}.txt"
        if p.exists():
            try:
                txt = p.read_text(encoding="utf-8", errors="ignore")
                chunks.append(txt[:4000])  # tighter than before
            except Exception:
                pass

    return "\n\n".join(chunks).strip()


async def call_llm_for_review(req) -> str:
    """
    Used by /analyze. Defensive clipping reduces timeouts and prevents
    extreme prompt truncation inside Ollama.
    """
    knowledge_context = await _load_knowledge_context(getattr(req, "knowledge_doc_ids", None))

    user_payload = {
        "document_name": getattr(req, "document_name", None),
        # keep text bounded (frontend already slices, but double-protect)
        "text": _clip_text(getattr(req, "text", ""), 9000),
        "hits": [h.dict() for h in getattr(req, "hits", [])],
        "knowledge_context": _clip_text(knowledge_context, 6000),
        "prompt_override": getattr(req, "prompt_override", None),
    }

    system_prompt = _build_review_system_prompt(getattr(req, "prompt_override", None))
    model = _get_default_model()
    url = _get_ollama_api_url()

    if _is_chat_endpoint(url):
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": _clip_text(system_prompt, 7000)},
                {"role": "user", "content": _safe_json_dumps(user_payload, 9000)},
            ],
            "temperature": getattr(req, "temperature", None) or 0.2,
            "stream": False,
        }
    else:
        prompt = (
            f"{_clip_text(system_prompt, 7000)}\n\n"
            f"USER_PAYLOAD_JSON:\n{_safe_json_dumps(user_payload, 9000)}\n"
        )
        payload = {
            "model": model,
            "prompt": prompt,
            "temperature": getattr(req, "temperature", None) or 0.2,
            "stream": False,
        }

    content, input_tokens, output_tokens = await _ollama_post(payload, "contract-review")

    _log_llm_event(
        app="contract_review",
        endpoint="/analyze",
        provider="local_ollama",
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )

    return content


# ===================================================================
# QUESTIONNAIRE â€” SINGLE QUESTION (fallback path)
# ===================================================================

async def call_llm_question_single(question: str, similar_bank_entries: list) -> str:
    bank_items = [
        {
            "id": e.id,
            "text": e.text,
            "answer": e.answer,
            "primary_tag": getattr(e, "primary_tag", None),
            "frameworks": e.frameworks or [],
            "status": e.status,
            "rejection_reasons": getattr(e, "rejection_reasons", []),
        }
        for e in similar_bank_entries[:5]
    ]

    effective_posture = _get_effective_org_posture()

    user_payload = {
        "question": _clip_text(question, 2000),
        "question_bank_entries": bank_items,
        "org_posture": _clip_text(effective_posture, 4000),
        "instructions": (
            "Answer this single security/SCRM question using: "
            "similar bank entries, org_posture, and conservative "
            "NIST/DFARS-aligned reasoning. "
            "Return ONLY the answer text (no JSON, no explanation)."
        ),
    }

    system_prompt = _apply_prompt_override(
        QUESTIONNAIRE_SYSTEM_PROMPT + "\nRespond with ONLY the answer text (no JSON)."
    )

    model = _get_default_model()
    url = _get_ollama_api_url()

    if _is_chat_endpoint(url):
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": _clip_text(system_prompt, 6000)},
                {"role": "user", "content": _safe_json_dumps(user_payload, 6000)},
            ],
            "temperature": 0.2,
            "stream": False,
        }
    else:
        payload = {
            "model": model,
            "prompt": f"{_clip_text(system_prompt, 6000)}\n\n{_safe_json_dumps(user_payload, 6000)}",
            "temperature": 0.2,
            "stream": False,
        }

    content, input_tokens, output_tokens = await _ollama_post(payload, "questionnaire-single")

    _log_llm_event(
        app="questionnaire_single",
        endpoint="/questionnaire/analyze",
        provider="local_ollama",
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )

    return content


# ===================================================================
# QUESTIONNAIRE â€” BATCH (robust JSON handling + org_posture)
# ===================================================================

async def call_llm_question_batch(questions_payload: list, knowledge_context: str) -> dict:
    effective_posture = _get_effective_org_posture()

    user_payload = {
        "questions": questions_payload,
        "knowledge_context": _clip_text(knowledge_context, 6000),
        "org_posture": _clip_text(effective_posture, 4000),
        "instructions": (
            'Return strict JSON: {"answers": '
            '[{"id":"...","answer":"...","confidence":0.85,"inferred_tags":["CUI"]}]}. '
            "No text outside JSON."
        ),
    }

    system_prompt = _apply_prompt_override(
        QUESTIONNAIRE_SYSTEM_PROMPT + "\nRespond ONLY with JSON. Do not add explanations."
    )

    model = _get_default_model()
    url = _get_ollama_api_url()

    if _is_chat_endpoint(url):
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": _clip_text(system_prompt, 7000)},
                {"role": "user", "content": _safe_json_dumps(user_payload, 9000)},
            ],
            "temperature": 0.2,
            "stream": False,
        }
    else:
        payload = {
            "model": model,
            "prompt": f"{_clip_text(system_prompt, 7000)}\n\n{_safe_json_dumps(user_payload, 9000)}",
            "temperature": 0.2,
            "stream": False,
        }

    raw, input_tokens, output_tokens = await _ollama_post(payload, "questionnaire-batch")

    if not raw.strip():
        raise HTTPException(
            status_code=502,
            detail="Batch LLM returned empty response for questionnaire-batch.",
        )

    try:
        data = json.loads(raw)
    except Exception:
        cleaned = raw.replace("```json", "").replace("```JSON", "").replace("```", "").strip()
        try:
            data = json.loads(cleaned)
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Batch LLM JSON parse failed: {exc}. Raw: {raw[:200]}",
            )

    answers = data.get("answers")
    if not isinstance(answers, list):
        raise HTTPException(status_code=502, detail="Batch LLM response missing 'answers' list")

    out: Dict[str, Dict[str, Any]] = {}

    for item in answers:
        if not isinstance(item, dict):
            continue

        qid = item.get("id")
        ans = item.get("answer")
        conf = item.get("confidence")

        if not qid or not isinstance(ans, str):
            continue

        try:
            conf_val = float(conf) if conf is not None else 0.6
            conf_val = max(0.0, min(conf_val, 1.0))
        except Exception:
            conf_val = 0.6

        inferred_tags = item.get("inferred_tags")
        if isinstance(inferred_tags, list):
            cleaned_tags = [str(t).strip() for t in inferred_tags if t]
        else:
            cleaned_tags = []

        out[str(qid)] = {
            "answer": ans.strip(),
            "confidence": conf_val,
            "inferred_tags": cleaned_tags,
        }

    _log_llm_event(
        app="questionnaire_batch",
        endpoint="/questionnaire/analyze",
        provider="local_ollama",
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )

    return out



