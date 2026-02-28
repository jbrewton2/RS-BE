from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import boto3


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RagJobStore:
    """
    DynamoDB-backed job store for async RAG analyze.

    Required env:
      RAG_JOBS_TABLE = DynamoDB table name

    Table schema:
      PK: job_id (S)
      Attributes: status, created_at, updated_at, progress_pct, message, error, result, ttl, review_id, request
    """
    def __init__(self) -> None:
        table_name = os.environ.get("RAG_JOBS_TABLE", "").strip()
        if not table_name:
            raise RuntimeError("RAG_JOBS_TABLE env var is required for async analyze jobs.")
        self._ddb = boto3.resource("dynamodb")
        self._table = self._ddb.Table(table_name)

    def create(self, job_id: str, review_id: str, request_obj: Dict[str, Any], ttl_seconds: int = 86400) -> None:
        now = _utc_iso()
        ttl = int(time.time()) + int(ttl_seconds)
        self._table.put_item(
            Item={
                "job_id": job_id,
                "review_id": str(review_id),
                "status": "queued",
                "created_at": now,
                "updated_at": now,
                "progress_pct": 0,
                "message": "queued",
                "request": json.dumps(request_obj),
                "ttl": ttl,
            }
        )

    def update(self, job_id: str, *, status: Optional[str] = None, progress_pct: Optional[int] = None,
               message: Optional[str] = None, error: Optional[str] = None) -> None:
        now = _utc_iso()

        expr_parts = ["#ua = :u"]
        vals: Dict[str, Any] = {":u": now}
        names: Dict[str, str] = {"#ua": "updated_at"}

        if status is not None:
            expr_parts.append("#st = :s")
            vals[":s"] = status
            names["#st"] = "status"
        if progress_pct is not None:
            expr_parts.append("#pp = :p")
            vals[":p"] = int(progress_pct)
            names["#pp"] = "progress_pct"
        if message is not None:
            expr_parts.append("#msg = :m")
            vals[":m"] = message
            names["#msg"] = "message"
        if error is not None:
            expr_parts.append("#err = :e")
            vals[":e"] = error
            names["#err"] = "error"

        self._table.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET " + ", ".join(expr_parts),
            ExpressionAttributeValues=vals,
            ExpressionAttributeNames=names,
        )

    def put_result(self, job_id: str, result_obj: Dict[str, Any]) -> None:
        now = _utc_iso()
        self._table.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET #res = :r, #st = :s, #pp = :p, #msg = :m, #ua = :u",
            ExpressionAttributeNames={
                "#res": "result",
                "#st": "status",
                "#pp": "progress_pct",
                "#msg": "message",
                "#ua": "updated_at",
            },
            ExpressionAttributeValues={
                ":r": json.dumps(result_obj),
                ":s": "succeeded",
                ":p": 100,
                ":m": "succeeded",
                ":u": now,
            },
        )

    def get(self, job_id: str) -> Dict[str, Any]:
        resp = self._table.get_item(Key={"job_id": job_id})
        item = resp.get("Item")
        if not item:
            raise KeyError(job_id)
        return item

