from __future__ import annotations

from typing import Protocol, runtime_checkable, Dict, Any, Optional


@runtime_checkable
class JobRunner(Protocol):
    """
    Async job execution abstraction.

    Phase 0: interface only (not wired).
    """

    def submit(self, job_type: str, payload: Dict[str, Any]) -> str: ...

    def status(self, job_id: str) -> Dict[str, Any]: ...

    def result(self, job_id: str) -> Optional[Dict[str, Any]]: ...
