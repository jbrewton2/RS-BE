def get_doc_original_key(doc_id: str) -> str:
    # Match whatever you already use to store uploaded docs
    # If your doc_id already includes filename, keep it.
    return f"docs/{doc_id}"

def get_doc_rendition_key(doc_id: str) -> str:
    return f"renditions/{doc_id}.pdf"
