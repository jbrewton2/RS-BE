from __future__ import annotations

from typing import Any, Dict, Optional
import uuid

from providers.jobs import JobRunner


class LocalInlineJobRunner(JobRunner):
    """
    Phase 0.75: Local-only placeholder.
    Returns job ids, but does not execute anything yet.
    """

    def submit(self, job_type: str, payload: Dict[str, Any]) -> str:
        return f"local-{job_type}-{uuid.uuid4()}"

    def status(self, job_id: str) -> Dict[str, Any]:
        return {"job_id": job_id, "state": "not_implemented"}

    def result(self, job_id: str) -> Optional[Dict[str, Any]]:
        return None

