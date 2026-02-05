# backend/documents/convert.py
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional


class DocConversionError(Exception):
    pass


def _find_soffice() -> str:
    # Common names/paths depending on distro/container
    candidates = [
        os.environ.get("SOFFICE_PATH", ""),
        "soffice",
        "/usr/bin/soffice",
        "/usr/lib/libreoffice/program/soffice",
    ]
    for c in candidates:
        if not c:
            continue
        if shutil.which(c) or Path(c).exists():
            return c
    return "soffice"  # last resort; will fail clearly if missing


def convert_docx_bytes_to_pdf_bytes(
    docx_bytes: bytes,
    *,
    timeout_seconds: int = 90,
    work_root: str = "/tmp/css-doc-conversion",
) -> Optional[bytes]:
    """
    Returns PDF bytes or None if conversion fails.
    Must be safe for production: no shell=True, temp isolation, bounded timeout.
    """
    soffice = _find_soffice()

    try:
        Path(work_root).mkdir(parents=True, exist_ok=True)
    except Exception:
        # If we can't create our preferred root, fall back to system temp.
        work_root = tempfile.gettempdir()

    with tempfile.TemporaryDirectory(prefix="css-docx2pdf-", dir=work_root) as td:
        td_path = Path(td)
        in_path = td_path / "input.docx"
        out_dir = td_path / "out"
        out_dir.mkdir(parents=True, exist_ok=True)

        in_path.write_bytes(docx_bytes)

        # LibreOffice writes output using the input base name.
        expected_pdf = out_dir / "input.pdf"

        # Headless conversion command (authoritative)
        cmd = [
            soffice,
            "--headless",
            "--nologo",
            "--nofirststartwizard",
            "--norestore",
            "--convert-to",
            "pdf",
            "--outdir",
            str(out_dir),
            str(in_path),
        ]

        # Harden environment: avoid profile writes outside temp, reduce surprises.
        env = os.environ.copy()
        env["HOME"] = str(td_path)
        env["TMPDIR"] = str(td_path)

        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return None
        except Exception:
            return None

        if proc.returncode != 0:
            return None

        if not expected_pdf.exists():
            # Some LO builds output different naming; find any pdf in out_dir.
            pdfs = list(out_dir.glob("*.pdf"))
            if not pdfs:
                return None
            expected_pdf = pdfs[0]

        try:
            return expected_pdf.read_bytes()
        except Exception:
            return None
