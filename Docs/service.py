import os
import tempfile
import asyncio
from typing import Optional

# These storage methods are assumptions; adapt to your StorageProvider interface.
# You need: read_bytes(key) and write_bytes(key, bytes, content_type)
# and a way to find the "original doc key" by doc_id.
from docs.store import get_doc_original_key, get_doc_rendition_key  # add small helpers

async def _run(cmd: list[str]) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed ({proc.returncode}): {' '.join(cmd)}\n{err.decode('utf-8','ignore')}")

async def get_or_create_pdf_rendition(storage, doc_id: str) -> Optional[bytes]:
    """
    If rendition exists -> return it.
    Else:
      - download original
      - if already PDF -> store as rendition and return
      - if DOCX -> convert to PDF via LibreOffice, store and return
    """
    rendition_key = get_doc_rendition_key(doc_id)  # e.g. f"renditions/{doc_id}.pdf"
    existing = await storage.read_bytes(rendition_key)
    if existing:
        return existing

    original_key = get_doc_original_key(doc_id)  # e.g. f"docs/{doc_id}"
    src = await storage.read_bytes(original_key)
    if not src:
        return None

    # quick sniff: PDF header
    if src[:4] == b"%PDF":
        await storage.write_bytes(rendition_key, src, content_type="application/pdf")
        return src

    # otherwise assume DOCX (you can sniff zip header PK.. and docx structure if you want)
    with tempfile.TemporaryDirectory() as td:
        in_path = os.path.join(td, f"{doc_id}.docx")
        out_dir = td
        with open(in_path, "wb") as f:
            f.write(src)

        # LibreOffice conversion (headless)
        # Produces <doc_id>.pdf in out_dir
        await _run([
            "soffice",
            "--headless",
            "--nologo",
            "--nolockcheck",
            "--nodefault",
            "--nofirststartwizard",
            "--convert-to",
            "pdf",
            "--outdir",
            out_dir,
            in_path,
        ])

        pdf_path = os.path.join(out_dir, f"{doc_id}.pdf")
        if not os.path.exists(pdf_path):
            return None

        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()

        await storage.write_bytes(rendition_key, pdf_bytes, content_type="application/pdf")
        return pdf_bytes
