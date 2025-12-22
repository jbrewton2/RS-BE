# backend/knowledge/service.py
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import List, Dict

from fastapi import HTTPException

from backend.core.config import KNOWLEDGE_STORE_FILE, KNOWLEDGE_DOCS_DIR
from backend.knowledge.models import KnowledgeDocMeta


# ---------------------------------------------------------------------
# Internal JSON storage helpers
# ---------------------------------------------------------------------

def _read_knowledge_store() -> List[Dict]:
    """
    Read the knowledge_store.json file (list of dicts).
    Returns [] if file missing or invalid.
    """
    if not os.path.exists(KNOWLEDGE_STORE_FILE):
        return []
    try:
        with open(KNOWLEDGE_STORE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_knowledge_store(entries: List[Dict]) -> None:
    """
    Write the entire knowledge store back to disk.
    """
    with open(KNOWLEDGE_STORE_FILE, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)


def _new_knowledge_doc_id(existing: List[Dict]) -> str:
    """
    Generate a simple incremental id: kd-1, kd-2, etc.
    """
    return f"kd-{len(existing) + 1}"


# ---------------------------------------------------------------------
# Public APIs: list, get, save
# ---------------------------------------------------------------------

def list_docs() -> List[KnowledgeDocMeta]:
    """
    Return all knowledge documents as KnowledgeDocMeta objects.
    """
    raw = _read_knowledge_store()
    docs: List[KnowledgeDocMeta] = []
    for item in raw:
        try:
            docs.append(KnowledgeDocMeta(**item))
        except Exception:
            # Skip malformed entries
            continue
    return docs


def get_doc(doc_id: str) -> KnowledgeDocMeta:
    """
    Return a single KnowledgeDocMeta by id, or raise.
    """
    raw = _read_knowledge_store()
    for item in raw:
        if item.get("id") == doc_id:
            try:
                return KnowledgeDocMeta(**item)
            except Exception as exc:
                raise HTTPException(
                    status_code=500,
                    detail=f"Corrupt knowledge entry for {doc_id}: {exc}",
                )
    raise HTTPException(status_code=404, detail="Knowledge document not found")


def save_doc(
    filename: str,
    text: str,
    doc_type: str | None = None,
    tags: List[str] | None = None,
) -> KnowledgeDocMeta:
    """
    Save a new knowledge document's text and metadata.

    - Stores extracted text via StorageProvider as: knowledge_docs/<id>.txt
    - Appends metadata into knowledge_store.json
    """
    raw_store = _read_knowledge_store()

    new_id = _new_knowledge_doc_id(raw_store)
    safe_name = f"{new_id}.txt"

    # Store extracted text via StorageProvider (preferred)
    storage = get_providers().storage
    key = f"knowledge_docs/{safe_name}"

    try:
        storage.put_object(
            key=key,
            data=text.encode("utf-8", errors="ignore"),
            content_type="text/plain",
            metadata=None,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to save knowledge doc text: {exc}",
        )

    meta = KnowledgeDocMeta(
        id=new_id,
        title=filename or new_id,
        filename=safe_name,
        doc_type=doc_type,
        tags=tags or [],
        created_at=datetime.now().isoformat(),
        size_bytes=len(text.encode("utf-8", errors="ignore")),
    )

    raw_store.append(meta.model_dump())
    _write_knowledge_store(raw_store)

    return meta



# ---------------------------------------------------------------------
# Helpers for building question context (for LLM use)
# ---------------------------------------------------------------------

def _load_knowledge_docs_meta() -> List[KnowledgeDocMeta]:
    """
    Internal helper: get all KnowledgeDocMeta entries, skipping malformed.
    """
    raw = _read_knowledge_store()
    docs: List[KnowledgeDocMeta] = []
    for item in raw:
        try:
            docs.append(KnowledgeDocMeta(**item))
        except Exception:
            continue
    return docs


def _load_knowledge_doc_text(doc_meta: KnowledgeDocMeta) -> str:
    """
    Internal helper: load text for a given knowledge doc.

    Preferred: StorageProvider key "knowledge_docs/<filename>"
    Fallback: legacy filesystem under KNOWLEDGE_DOCS_DIR
    """
    # 1) StorageProvider (preferred)
    try:
        storage = get_providers().storage
        key = f"knowledge_docs/{doc_meta.filename}"
        data = storage.get_object(key)
        return data.decode("utf-8", errors="ignore")
    except Exception:
        pass

    # 2) Legacy filesystem fallback
    path = os.path.join(KNOWLEDGE_DOCS_DIR, doc_meta.filename)
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def build_context_for_question(
    question_text: str,
    max_docs: int = 3,
) -> List[dict]:
    """
    Very simple retrieval: compute crude token overlap between the question
    text and each knowledge doc's text, and return the top few docs.

    Returns a list of:
       {
         "source": <title>,
         "doc_id": <id>,
         "doc_type": <doc_type>,
         "score": <overlap score>,
         "excerpt": <first ~1000 chars of doc>,
       }
    """
    docs = _load_knowledge_docs_meta()
    if not docs:
        return []

    q_tokens = set(question_text.lower().split())
    if not q_tokens:
        return []

    scored: List[tuple[float, KnowledgeDocMeta]] = []

    for meta in docs:
        text = _load_knowledge_doc_text(meta)
        if not text.strip():
            continue
        doc_tokens = set(text.lower().split())
        if not doc_tokens:
            continue
        overlap = len(q_tokens & doc_tokens) / max(1, len(q_tokens))
        if overlap > 0:
            scored.append((overlap, meta))

    if not scored:
        return []

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:max_docs]

    results: List[dict] = []
    for score, meta in top:
        text = _load_knowledge_doc_text(meta)
        excerpt = text[:1000]  # keep it short; you can refine later
        results.append(
            {
                "source": meta.title,
                "doc_id": meta.id,
                "doc_type": meta.doc_type,
                "score": score,
                "excerpt": excerpt,
            }
        )

    return results
