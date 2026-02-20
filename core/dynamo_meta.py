# core/dynamo_meta.py
from __future__ import annotations

import os
import time
import json
import hashlib
from typing import Any, Dict, List, Optional

import boto3


def _now_iso() -> str:
    # keep simple + deterministic formatting
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()


class DynamoMeta:
    """
    Minimal DynamoDB metadata writer/reader for CSS mock.
    Single-table design: pk/sk.

    REVIEW meta:
      pk = REVIEW#{review_id}
      sk = META

    Review doc:
      pk = REVIEW#{review_id}
      sk = DOC#{doc_id}

    RAG run:
      pk = REVIEW#{review_id}
      sk = RAGRUN#{run_id}
    """

    def __init__(self, table_name: Optional[str] = None):
        self.table_name = table_name or os.environ.get("DYNAMODB_TABLE", "")
        if not self.table_name:
            raise RuntimeError("DYNAMODB_TABLE env var is missing")

        self.ddb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"))
        self.table = self.ddb.Table(self.table_name)

    def upsert_review_meta(
        self,
        review_id: str,
        *,
        pdf_key: Optional[str] = None,
        pdf_sha256: Optional[str] = None,
        pdf_size: Optional[int] = None,
        extract_text_key: Optional[str] = None,
        extract_text_sha256: Optional[str] = None,
        extract_json_key: Optional[str] = None,
        extract_json_sha256: Optional[str] = None,
    ) -> Dict[str, Any]:
        pk = f"REVIEW#{review_id}"
        sk = "META"
        now = _now_iso()

        # Build update expression dynamically
        sets = ["updated_at = :u"]
        vals: Dict[str, Any] = {":u": now, ":c": now}
        names: Dict[str, str] = {}

        # ensure created_at set once
        sets.append("created_at = if_not_exists(created_at, :c)")

        def add(name: str, value: Any):
            if value is None:
                return
            # avoid reserved words
            ph = f"#{name}"
            names[ph] = name
            vals[f":{name}"] = value
            sets.append(f"{ph} = :{name}")

        add("review_id", review_id)
        add("pdf_s3_key", pdf_key)
        add("pdf_sha256", pdf_sha256)
        add("pdf_size", pdf_size)
        add("extract_text_s3_key", extract_text_key)
        add("extract_text_sha256", extract_text_sha256)
        add("extract_json_s3_key", extract_json_key)
        add("extract_json_sha256", extract_json_sha256)

        update_expr = "SET " + ", ".join(sets)

        resp = self.table.update_item(
            Key={"pk": pk, "sk": sk},
            UpdateExpression=update_expr,
            ExpressionAttributeValues=vals,
            ExpressionAttributeNames=names if names else None,
            ReturnValues="ALL_NEW",
        )
        return resp.get("Attributes") or {}

    def list_reviews(self) -> List[Dict[str, Any]]:
        # Scan is OK for mock; for prod add GSI.
        resp = self.table.scan(
            FilterExpression="begins_with(pk, :p) AND sk = :sk",
            ExpressionAttributeValues={":p": "REVIEW#", ":sk": "META"},
        )
        return resp.get("Items") or []

    def get_review_meta(self, review_id: str) -> Optional[Dict[str, Any]]:
        resp = self.table.get_item(Key={"pk": f"REVIEW#{review_id}", "sk": "META"})
        return resp.get("Item")

    def put_rag_run(self, review_id: str, run_id: str, *, rag_key: str, rag_sha256: str, params_hash: str, analysis_intent: str, context_profile: str, top_k: int) -> None:
        now = _now_iso()
        self.table.put_item(
            Item={
                "pk": f"REVIEW#{review_id}",
                "sk": f"RAGRUN#{run_id}",
                "review_id": review_id,
                "run_id": run_id,
                "rag_s3_key": rag_key,
                "rag_sha256": rag_sha256,
                "params_hash": params_hash,
                "analysis_intent": analysis_intent,
                "context_profile": context_profile,
                "top_k": int(top_k),
                "created_at": now,
            }
        )