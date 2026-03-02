import os
import sys
from typing import List

# Ensure repo root is on sys.path for local imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


def die(msg: str, code: int = 2) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(code)

def main() -> int:
    # Env must match running cluster config (you can set locally before running)
    endpoint = os.getenv("OPENSEARCH_ENDPOINT")
    index = os.getenv("OPENSEARCH_INDEX")
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    review_id = os.getenv("REVIEW_ID")

    if not endpoint or not index or not region or not review_id:
        die("Missing env. Need OPENSEARCH_ENDPOINT, OPENSEARCH_INDEX, AWS_REGION (or AWS_DEFAULT_REGION), REVIEW_ID")

    # Import the same client used by the app
    from providers.impl.vector_opensearch import OpenSearchVectorStore

    # Instantiate and force-config (avoid relying on settings loader)
    vs = OpenSearchVectorStore()
    vs.endpoint = endpoint
    vs.index = index

    # Build client (uses AWS4Auth via boto3 creds)
    client = vs.client  # property should lazily build

    # Probe terms
    terms: List[str] = [
        "submit", "submission", "submittal",
        "deliverable", "deliverables",
        "due date", "no later than", "within", "calendar days",
        "CDRL", "DID", "data item description",
        "format", "template", "revision history",
        "government acceptance", "resubmit",
    ]

    # Use a bool should query scoped to review_id; match against chunk_text
    body = {
        "size": 10,
        "_source": ["review_id", "doc_name", "chunk_id", "chunk_text"],
        "query": {
            "bool": {
                "filter": [{"term": {"review_id": str(review_id)}}],
                "should": [{"match_phrase": {"chunk_text": t}} for t in terms],
                "minimum_should_match": 1
            }
        }
    }

    resp = client.search(index=index, body=body)
    hits = (((resp or {}).get("hits") or {}).get("hits") or [])
    print(f"hits={len(hits)} (showing up to 10)")

    for h in hits:
        src = h.get("_source") or {}
        doc = src.get("doc_name") or ""
        cid = src.get("chunk_id") or ""
        txt = (src.get("chunk_text") or "").replace("\r", " ").replace("\n", " ")
        txt = txt[:300]
        print(f"- doc={doc} chunk_id={cid} text={txt}")

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
