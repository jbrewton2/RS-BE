# core/dynamo_meta.py
from __future__ import annotations

import hashlib
import os
import time
from typing import Any, Dict, List, Optional
from uuid import uuid4

import boto3
from boto3.dynamodb.conditions import Key


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

        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or None
        self.ddb = boto3.resource("dynamodb", region_name=region)
        self.table = self.ddb.Table(self.table_name)

    # ----------------------------
    # Internal helpers
    # ----------------------------
    def _pk(self, review_id: str) -> str:
        return f"REVIEW#{review_id}"

    def _delete_all_review_docs(self, review_id: str) -> int:
        """
        Replace semantics helper:
        Delete all DOC# child items for a review.

        Returns number of DOC items deleted.
        """
        review_id = (review_id or "").strip()
        if not review_id:
            return 0

        pk = self._pk(review_id)
        deleted = 0

        # Query all existing DOC# items (paginate)
        last_evaluated_key = None
        to_delete: List[Dict[str, str]] = []

        while True:
            kwargs: Dict[str, Any] = {
                "KeyConditionExpression": Key("pk").eq(pk) & Key("sk").begins_with("DOC#"),
                "ProjectionExpression": "pk, sk",
            }
            if last_evaluated_key:
                kwargs["ExclusiveStartKey"] = last_evaluated_key

            resp = self.table.query(**kwargs)
            items = resp.get("Items") or []
            for it in items:
                pkv = it.get("pk")
                skv = it.get("sk")
                if pkv and skv:
                    to_delete.append({"pk": pkv, "sk": skv})

            last_evaluated_key = resp.get("LastEvaluatedKey")
            if not last_evaluated_key:
                break

        if not to_delete:
            return 0

        # Batch delete
        with self.table.batch_writer() as batch:
            for key in to_delete:
                batch.delete_item(Key=key)
                deleted += 1

        return deleted

    # ----------------------------
    # Public API
    # ----------------------------
    def upsert_review_docs(self, review_id: str, docs: List[Dict[str, Any]]) -> int:
        """
        Persist per-doc metadata as child items:
          pk = REVIEW#<id>
          sk = DOC#<doc_id>

        Replace semantics:
          - Delete all existing DOC# items for this review_id
          - Write the provided docs list as the new truth

        NOTE: Do NOT store full doc content here.
        Store pointers + small metadata only.
        Returns the count of docs written.
        """
        review_id = (review_id or "").strip()
        if not review_id:
            return 0

        pk = self._pk(review_id)
        now = _now_iso()

        # 1) DELETE old DOC# children to prevent duplication / zombie docs
        self._delete_all_review_docs(review_id)

        # 2) WRITE new DOC# children
        written = 0
        to_write: List[Dict[str, Any]] = []

        for d in (docs or []):
            if not isinstance(d, dict):
                continue

            doc_id = (d.get("id") or d.get("doc_id") or "").strip()
            if not doc_id:
                doc_id = str(uuid4())

            name = (d.get("name") or d.get("filename") or d.get("title") or "Document").strip()
            filename = (d.get("filename") or d.get("name") or "").strip() or None
            mime_type = (d.get("mimeType") or d.get("mime_type") or "").strip() or None

            size_bytes = d.get("size_bytes")
            if size_bytes is None:
                size_bytes = d.get("sizeBytes")
            try:
                size_bytes = int(size_bytes) if size_bytes is not None else None
            except Exception:
                size_bytes = None

            pdf_url = (d.get("pdf_url") or d.get("pdfUrl") or "").strip() or None
            pdf_s3_key = (d.get("pdf_s3_key") or d.get("pdfKey") or d.get("pdf_key") or "").strip() or None

            item = {
                "pk": pk,
                "sk": f"DOC#{doc_id}",
                "review_id": review_id,
                "doc_id": doc_id,
                "id": doc_id,
                "name": name,
                "filename": filename,
                "mimeType": mime_type,
                "size_bytes": size_bytes,
                "pdf_url": pdf_url,
                "pdf_s3_key": pdf_s3_key,
                "created_at": (d.get("created_at") or d.get("createdAt") or now),
                "updated_at": now,
            }

            # drop Nones to keep Dynamo items clean
            item = {k: v for k, v in item.items() if v is not None}
            to_write.append(item)

        if not to_write:
            return 0

        with self.table.batch_writer() as batch:
            for item in to_write:
                batch.put_item(Item=item)
                written += 1

        return written

    def list_review_docs(self, review_id: str) -> List[Dict[str, Any]]:
        review_id = (review_id or "").strip()
        if not review_id:
            return []
        pk = self._pk(review_id)
        resp = self.table.query(
            KeyConditionExpression=Key("pk").eq(pk) & Key("sk").begins_with("DOC#")
        )
        return resp.get("Items") or []

    def get_review_detail(self, review_id: str) -> Optional[Dict[str, Any]]:
        review_id = (review_id or "").strip()
        if not review_id:
            return None
        meta = self.get_review_meta(review_id)
        if not meta:
            return None
        docs = self.list_review_docs(review_id)
        meta["docs"] = docs
        meta["doc_count"] = int(meta.get("doc_count") or len(docs))
        return meta

    def upsert_review_meta(
        self,
        review_id: str,
        *,
        review: Optional[Dict[str, Any]] = None,
        pdf_key: Optional[str] = None,
        pdf_sha256: Optional[str] = None,
        pdf_size: Optional[int] = None,
        extract_text_key: Optional[str] = None,
        extract_text_sha256: Optional[str] = None,
        extract_json_key: Optional[str] = None,
        extract_json_sha256: Optional[str] = None,
    ) -> Dict[str, Any]:
        review_id = (review_id or "").strip()
        if not review_id:
            return {}

        pk = self._pk(review_id)
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
            ph = f"#{name}"
            names[ph] = name
            vals[f":{name}"] = value
            sets.append(f"{ph} = :{name}")

        add("review_id", review_id)

        # Optional "Review Contract" fields
        if isinstance(review, dict):
            title = (review.get("title") or review.get("name") or "").strip() or None
            status = (review.get("status") or "").strip() or None
            if not status:
                status = "Draft"

            department = (review.get("department") or "").strip() or None
            reviewer = (review.get("reviewer") or "").strip() or None

            # Canonical: data_type (snake_case). Accept dataType as input.
            data_type = (review.get("data_type") or review.get("dataType") or "").strip() or None

            docs = review.get("docs") or []
            try:
                doc_count = int(
                    review.get("doc_count")
                    or review.get("docCount")
                    or (len(docs) if isinstance(docs, list) else 0)
                )
            except Exception:
                doc_count = len(docs) if isinstance(docs, list) else 0

            add("title", title)
            add("status", status)
            add("department", department)
            add("reviewer", reviewer)
            add("data_type", data_type)
            add("doc_count", int(doc_count))

            # Persist AI analysis outputs (UI contract)
            # NOTE: Dynamo item size is limited (~400KB). Keep payload bounded.
            last_analysis_at = review.get("lastAnalysisAt") or review.get("last_analysis_at") or None
            if isinstance(last_analysis_at, str) and last_analysis_at.strip():
                add("lastAnalysisAt", last_analysis_at.strip())

            ai_summary = review.get("aiSummary")
            if isinstance(ai_summary, str) and ai_summary.strip():
                add("aiSummary", ai_summary.strip()[:50000])

            ai_risks = review.get("aiRisks")
            if isinstance(ai_risks, list):
                # cap to avoid oversized items
                add("aiRisks", ai_risks[:200])

            rag = review.get("rag")
            if isinstance(rag, dict):
                # store a compact RAG blob only
                rag_compact: Dict[str, Any] = {}
                s = rag.get("summary")
                if isinstance(s, str) and s.strip():
                    rag_compact["summary"] = s.strip()[:50000]
                rc = rag.get("retrieved_counts")
                if isinstance(rc, dict):
                    rag_compact["retrieved_counts"] = rc
                w = rag.get("warnings")
                if isinstance(w, list):
                    rag_compact["warnings"] = w[:50]
                st = rag.get("stats")
                if isinstance(st, dict):
                    rag_compact["stats"] = st
                secs = rag.get("sections")
                if isinstance(secs, list):
                    safe_secs: List[Dict[str, Any]] = []
                    for sec in secs[:30]:
                        if not isinstance(sec, dict):
                            continue
                        safe_secs.append({
                            "id": sec.get("id"),
                            "title": sec.get("title"),
                            "owner": sec.get("owner"),
                            "findings": (sec.get("findings") or [])[:10] if isinstance(sec.get("findings"), list) else sec.get("findings"),
                            "gaps": (sec.get("gaps") or [])[:10] if isinstance(sec.get("gaps"), list) else sec.get("gaps"),
                            "recommended_actions": (sec.get("recommended_actions") or [])[:10] if isinstance(sec.get("recommended_actions"), list) else sec.get("recommended_actions"),
                            "evidence": (sec.get("evidence") or [])[:10] if isinstance(sec.get("evidence"), list) else sec.get("evidence"),
                        })
                    rag_compact["sections"] = safe_secs
                add("rag", rag_compact)
            # Optional: store a compact autoFlags summary only (avoid large payloads)
            auto_flags = review.get("autoFlags")
            if isinstance(auto_flags, dict):
                summary = auto_flags.get("summary")
                if isinstance(summary, str) and summary.strip():
                    add("autoFlags_summary", summary.strip()[:2000])

        add("pdf_s3_key", pdf_key)
        add("pdf_sha256", pdf_sha256)
        add("pdf_size", pdf_size)
        add("extract_text_s3_key", extract_text_key)
        add("extract_text_sha256", extract_text_sha256)
        add("extract_json_s3_key", extract_json_key)
        add("extract_json_sha256", extract_json_sha256)

        update_expr = "SET " + ", ".join(sets)

        kwargs: Dict[str, Any] = {
            "Key": {"pk": pk, "sk": sk},
            "UpdateExpression": update_expr,
            "ExpressionAttributeValues": vals,
            "ReturnValues": "ALL_NEW",
        }
        if names:
            kwargs["ExpressionAttributeNames"] = names

        resp = self.table.update_item(**kwargs)
        return resp.get("Attributes") or {}

    def list_reviews(self) -> List[Dict[str, Any]]:
        # Scan is OK for mock; for prod add GSI.
        resp = self.table.scan(
            FilterExpression="begins_with(pk, :p) AND sk = :sk",
            ExpressionAttributeValues={":p": "REVIEW#", ":sk": "META"},
        )
        items = resp.get("Items") or []
        for it in items:
            if "doc_count" not in it:
                it["doc_count"] = 0
        return items

    def get_review_meta(self, review_id: str) -> Optional[Dict[str, Any]]:
        review_id = (review_id or "").strip()
        if not review_id:
            return None
        resp = self.table.get_item(Key={"pk": self._pk(review_id), "sk": "META"})
        return resp.get("Item")

    def put_rag_run(
        self,
        review_id: str,
        run_id: str,
        *,
        rag_key: str,
        rag_sha256: str,
        params_hash: str,
        analysis_intent: str,
        context_profile: str,
        top_k: int,
    ) -> None:
        review_id = (review_id or "").strip()
        run_id = (run_id or "").strip()
        if not review_id or not run_id:
            return

        now = _now_iso()
        self.table.put_item(
            Item={
                "pk": self._pk(review_id),
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