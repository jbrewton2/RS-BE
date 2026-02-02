# backend/knowledge/router.py
from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, UploadFile, File, HTTPException, Form, Depends, Request
from core.deps import StorageDep
from fastapi.responses import FileResponse, PlainTextResponse, Response

from core.config import PdfReader, docx, KNOWLEDGE_STORE_FILE, KNOWLEDGE_DOCS_DIR
from knowledge.models import KnowledgeDocMeta, KnowledgeDocListResponse
from knowledge.service import list_docs, get_doc, save_doc

# ÃƒÂ¢Ã…â€œÃ¢â‚¬Â¦ AUTH
from auth.jwt import get_current_user

from core.providers import providers_from_request

router = APIRouter(
    prefix="/knowledge",
    tags=["knowledge"],
    # ÃƒÂ¢Ã…â€œÃ¢â‚¬Â¦ Enforce JWT on all /knowledge endpoints
    dependencies=[Depends(get_current_user)],
)

STORE_PATH = Path(KNOWLEDGE_STORE_FILE)
DOCS_DIR = Path(KNOWLEDGE_DOCS_DIR)


# ---------------------------------------------------------------------
# Internal helpers: metadata store + text extraction
# ---------------------------------------------------------------------

def _load_store() -> List[Dict[str, Any]]:
    """
    Load knowledge_store.json as a list of dicts.
    """
    if not STORE_PATH.exists():
        return []
    try:
        with STORE_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def _save_store(items: List[Dict[str, Any]]) -> None:
    """
    Save the full metadata list back to knowledge_store.json.
    """
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STORE_PATH.open("w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)


def _get_doc_meta_from_store(doc_id: str) -> Optional[Dict[str, Any]]:
    """
    Return metadata dict for a given doc_id from knowledge_store.json, or None.
    """
    for item in _load_store():
        if item.get("id") == doc_id:
            return item
    return None


def _extract_text_from_upload(file: UploadFile, data: bytes) -> str:
    """
    Simple extraction for PDF / DOCX / TXT uploads.

    - For PDF: uses PdfReader to extract text page-by-page.
    - For DOCX: uses python-docx to join paragraphs.
    - For others: assumes UTF-8 text.
    """
    filename = (file.filename or "").lower()

    # PDF
    if filename.endswith(".pdf"):
        if PdfReader is None:
            raise HTTPException(status_code=500, detail="PDF support not installed.")
        try:
            reader = PdfReader(BytesIO(data))
            chunks: List[str] = []
            for page in reader.pages:
                try:
                    txt = page.extract_text() or ""
                except Exception:
                    txt = ""
                if txt.strip():
                    chunks.append(txt)
            return "\n".join(chunks).strip() or "(No text extracted.)"
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to read PDF: {exc}")

    # DOCX
    if filename.endswith(".docx") or filename.endswith(".doc"):
        if docx is None:
            raise HTTPException(status_code=500, detail="DOCX support not installed.")
        try:
            document = docx.Document(BytesIO(data))
            paras = [p.text for p in document.paragraphs]
            return "\n".join(paras).strip() or "(No text extracted.)"
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to read DOCX: {exc}")

    # TXT / fallback
    try:
        return data.decode("utf-8", errors="ignore")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to decode text: {exc}")


# ---------------------------------------------------------------------
# GET /knowledge/docs   ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â list docs
# ---------------------------------------------------------------------

@router.get("/docs", response_model=KnowledgeDocListResponse)
async def list_knowledge_docs_route():
    """
    List all knowledge documents.

    Used by the KnowledgePage to populate the grid.
    """
    docs = list_docs()
    return KnowledgeDocListResponse(docs=docs)


# ---------------------------------------------------------------------
# GET /knowledge/docs/{doc_id}   ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â get one doc meta
# ---------------------------------------------------------------------

@router.get("/docs/{doc_id}", response_model=KnowledgeDocMeta)
async def get_knowledge_doc_route(doc_id: str):
    """
    Get metadata for a single knowledge document.
    """
    return get_doc(doc_id)


# ---------------------------------------------------------------------
# POST /knowledge/docs   ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â upload & ingest a doc
# ---------------------------------------------------------------------

@router.post("/docs", response_model=KnowledgeDocMeta)
async def upload_knowledge_doc_route(request: Request, file: UploadFile = File(...),
    doc_type: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),
):
    """
    Upload an SSP / prior questionnaire / policy / runbook into the knowledge base.

    Behavior:
    - Reads the file into memory.
    - Extracts text (PDF / DOCX / TXT).
    - Saves text to knowledge_docs/<id>.txt.
    - Saves metadata to knowledge_store.json, including doc_type + tags if provided.
    """
    try:
        data = await file.read()
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Failed to read uploaded file: {exc}",
        )

    if not data:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    text = _extract_text_from_upload(file, data)

    # Parse tags string "a, b, c" -> ["a", "b", "c"]
    tag_list: Optional[list[str]] = None
    if tags:
        parts = [t.strip() for t in tags.split(",")]
        tag_list = [p for p in parts if p]
    storage = providers.storage
    meta = save_doc(storage,
        filename=file.filename or "uploaded",
        text=text,
        doc_type=doc_type,
        tags=tag_list,
    )

    return meta


# ---------------------------------------------------------------------
# GET /knowledge/docs/{doc_id}/text ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â inline extracted text for viewer
# ---------------------------------------------------------------------

@router.get("/docs/{doc_id}/text", response_class=PlainTextResponse)
async def get_knowledge_doc_text(doc_id: str, storage=StorageDep):
    """
    Return the extracted text for a knowledge document as plain text.

    Preferred: StorageProvider key "knowledge_docs/<doc_id>.txt"
    Fallback: legacy filesystem under KNOWLEDGE_DOCS_DIR
    """
    meta = _get_doc_meta_from_store(doc_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Knowledge document not found")

    key = f"knowledge_docs/{doc_id}.txt"

    # 1) StorageProvider (preferred)
    try:
        data = storage.get_object(key)
        content = data.decode("utf-8", errors="ignore")
        return PlainTextResponse(content, media_type="text/plain")
    except FileNotFoundError:
        pass
    except Exception:
        # fall back to legacy filesystem
        pass

    # 2) Legacy filesystem fallback
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    text_path = DOCS_DIR / f"{doc_id}.txt"

    if not text_path.exists():
        raise HTTPException(status_code=404, detail="Extracted text file not found")

    try:
        with text_path.open("r", encoding="utf-8") as f:
            content = f.read()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read text file: {exc}")

    return PlainTextResponse(content, media_type="text/plain")# ---------------------------------------------------------------------
# GET /knowledge/docs/{doc_id}/file ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â download extracted text file
# ---------------------------------------------------------------------

@router.get("/docs/{doc_id}/file")
async def get_knowledge_doc_file(doc_id: str, storage=StorageDep):
    """
    Serve the extracted text file as a download (.txt).

    Preferred: StorageProvider key "knowledge_docs/<doc_id>.txt"
    Fallback: legacy filesystem under KNOWLEDGE_DOCS_DIR
    """
    meta = _get_doc_meta_from_store(doc_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Knowledge document not found")

    filename = (meta.get("title") or doc_id) + ".txt"
    key = f"knowledge_docs/{doc_id}.txt"

    # 1) StorageProvider (preferred)
    try:
        data = storage.get_object(key)
        headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
        return Response(content=data, media_type="text/plain", headers=headers)
    except FileNotFoundError:
        pass
    except Exception:
        pass

    # 2) Legacy filesystem fallback
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    text_path = DOCS_DIR / f"{doc_id}.txt"
    if not text_path.exists():
        raise HTTPException(status_code=404, detail="Extracted text file not found")

    return FileResponse(
        path=str(text_path),
        media_type="text/plain",
        filename=filename,
    )

@router.delete("/docs/{doc_id}")
async def delete_knowledge_doc_route(doc_id: str, storage=StorageDep):
    """
    Delete a knowledge document's metadata and extracted text file.
    """
    items = _load_store()
    new_items: list[dict] = []
    found = False

    for item in items:
        if item.get("id") == doc_id:
            found = True
            continue
        new_items.append(item)

    if not found:
        raise HTTPException(status_code=404, detail="Knowledge document not found")

    _save_store(new_items)

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    text_path = DOCS_DIR / f"{doc_id}.txt"

    # Best-effort delete from StorageProvider (preferred)
    try:
        storage.delete_object(f"knowledge_docs/{doc_id}.txt")
    except Exception:
        pass
    try:
        if text_path.exists():
            text_path.unlink()
    except Exception:
        # Do not fail delete if file removal has issues
        pass

    return {"ok": True}











