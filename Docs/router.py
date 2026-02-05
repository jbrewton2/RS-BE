from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from typing import Optional
import io

from storage.factory import get_storage  # adjust if your get_storage lives elsewhere
from docs.service import get_or_create_pdf_rendition  # we’ll add this

router = APIRouter()

@router.get("/docs/{doc_id}/pdf")
async def get_doc_pdf(doc_id: str, storage=Depends(get_storage)):
    """
    Returns a PDF rendition for a document (docx -> pdf or original pdf).
    """
    pdf_bytes: Optional[bytes] = await get_or_create_pdf_rendition(storage, doc_id)
    if not pdf_bytes:
        raise HTTPException(status_code=404, detail="PDF rendition not found")

    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Cache-Control": "no-store"},
    )
