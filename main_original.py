# backend/main.py
from __future__ import annotations

import json
import os
import re
from io import BytesIO
from typing import List, Optional, Literal

import httpx
import uvicorn
from fastapi import (
    FastAPI,
    UploadFile,
    File,
    HTTPException,
    Query,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

# ---------------- PDF / DOCX libs ---------------- #

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None  # type: ignore

try:
    import docx  # python-docx
except ImportError:
    docx = None  # type: ignore


# ---------------- Internal modules from old backend ---------------- #

from flags_store import (
    FlagRule,
    FlagsPayload,
    load_flags,
    save_flags,
)
from schemas import (
    AnalyzeRequestModel,
    AnalyzeResponseModel,
)
from llm_config_store import (
    LLMConfig,
    LLMProvider,
    load_llm_config,
    save_llm_config,
)

# ---------------- FastAPI app + CORS ---------------- #

app = FastAPI(title="Contract Security Studio Backend")

origins = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- Paths & config ---------------- #

BASE_DIR = os.path.dirname(__file__)
FILES_DIR = os.path.join(BASE_DIR, "files")
os.makedirs(FILES_DIR, exist_ok=True)

REVIEWS_FILE = os.path.join(BASE_DIR, "reviews.json")
QUESTION_BANK_PATH = os.path.join(BASE_DIR, "question_bank.json")
KNOWLEDGE_STORE_FILE = os.path.join(BASE_DIR, "knowledge_store.json")
KNOWLEDGE_DOCS_DIR = os.path.join(BASE_DIR, "knowledge_docs")
os.makedirs(KNOWLEDGE_DOCS_DIR, exist_ok=True)

OLLAMA_API_URL = os.getenv("OLLAMA_API_URL", "http://localhost:11434/api/chat")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")

RISK_CATEGORY_LIST = [
    "DATA_CLASSIFICATION",
    "CYBER_DFARS",
    "INCIDENT_REPORTING",
    "FLOWDOWN",
    "LIABILITY",
    "SLA",
    "TERMINATION",
    "IP",
    "PRIVACY",
    "OTHER",
]

SYSTEM_PROMPT_BASE = """
You are an expert contract, cybersecurity, and privacy reviewer for a large
U.S. government contractor.

You are reviewing one or more CONTRACT DOCUMENTS (RFP, RFI, SOW, PWS, draft
contract, attachments, etc.). Your job is to describe WHAT THESE DOCUMENTS
REQUIRE — not to describe a generic service or the contractor's general posture.

Return a concise, plain-text summary in the following sections:

OBJECTIVE
SCOPE
KEY REQUIREMENTS
KEY RISKS
GAPS AND AMBIGUITIES
RECOMMENDED NEXT STEPS
""".strip()

QUESTIONNAIRE_SYSTEM_PROMPT = """
You are answering SECURITY and SUPPLY-CHAIN RISK MANAGEMENT questionnaires
for a U.S. Government contractor. The organization maintains a NIST SP 800-171-
compliant enclave for CUI, follows DFARS 252.204-7012, and uses standard practices.

Your job:
- Provide precise, contract-safe answers to security/SCRM questions.
- Base answers on the QUESTION text and any similar QUESTION BANK ENTRIES.

STRICT RULES:
- Answer directly, in professional language.
- Do NOT mention AI or prompts.
- Do NOT include JSON, bullets, or headings unless explicitly requested.
- If you cannot answer, say that additional internal review is required.
""".strip()

# =====================================================================
# MODELS (simple versions)
# =====================================================================

class PageInfoModel(BaseModel):
    page_number: int
    start_line: int
    end_line: int


class ExtractResponseModel(BaseModel):
    text: str
    type: str
    pdf_url: Optional[str] = None
    pages: Optional[List[PageInfoModel]] = None


class ReviewSummaryResponse(BaseModel):
    summary: str
    risks: list = []


# Questionnaire models

AnswerSource = Literal["bank", "llm"]
QuestionStatus = Literal["auto_approved", "needs_review", "low_confidence"]
FeedbackStatus = Literal["approved", "rejected"]

class QuestionnaireQuestionModel(BaseModel):
    id: str
    question_text: str
    tags: Optional[List[str]] = None

    suggested_answer: Optional[str] = None
    confidence: Optional[float] = None
    answer_source: Optional[AnswerSource] = None

    status: Optional[QuestionStatus] = None
    matched_bank_id: Optional[str] = None

    feedback_status: Optional[FeedbackStatus] = None
    feedback_reason: Optional[str] = None

    knowledge_sources: Optional[List[dict]] = None

class QuestionnaireAnalyzeRequest(BaseModel):
    raw_text: str
    llm_enabled: bool = True


class QuestionnaireAnalysisResponse(BaseModel):
    raw_text: str
    questions: List[QuestionnaireQuestionModel]
    overall_confidence: Optional[float] = None


class QuestionnaireExtractResponse(BaseModel):
    raw_text: str


class QuestionBankEntryModel(BaseModel):
    id: str
    text: str
    answer: str
    primary_tag: Optional[str] = None
    frameworks: Optional[List[str]] = None
    status: Optional[str] = "approved"


class QuestionBankUpsertModel(BaseModel):
    id: Optional[str] = None
    text: str
    answer: str
    primaryTag: Optional[str] = None
    frameworks: Optional[List[str]] = None
    status: Optional[str] = "approved"


class QuestionnaireFeedbackRequest(BaseModel):
    question_id: str
    matched_bank_id: Optional[str] = None
    approved: bool
    feedback_reason: Optional[str] = None
    final_answer: Optional[str] = None

# Knowledge center models

class KnowledgeDocMeta(BaseModel):
    id: str
    title: str
    filename: str
    doc_type: Optional[str] = None  # e.g. "SSP", "Questionnaire", "Policy"
    tags: List[str] = []
    created_at: str  # ISO timestamp
    size_bytes: int


class KnowledgeDocListResponse(BaseModel):
    docs: List[KnowledgeDocMeta]



# =====================================================================
# REVIEW STORAGE (unchanged)
# =====================================================================

def _read_reviews_file() -> List[dict]:
    if not os.path.exists(REVIEWS_FILE):
        return []
    try:
        with open(REVIEWS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_reviews_file(reviews: List[dict]) -> None:
    with open(REVIEWS_FILE, "w", encoding="utf-8") as f:
        json.dump(reviews, f, indent=2, ensure_ascii=False)

# =====================================================================
# KNOWLEDGE STORE HELPERS
# =====================================================================

def _read_knowledge_store() -> List[dict]:
    if not os.path.exists(KNOWLEDGE_STORE_FILE):
        return []
    try:
        with open(KNOWLEDGE_STORE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_knowledge_store(entries: List[dict]) -> None:
    with open(KNOWLEDGE_STORE_FILE, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)


def _new_knowledge_doc_id(existing: List[dict]) -> str:
    # simple incremental id
    return f"kd-{len(existing) + 1}"

# =====================================================================
# SIMPLE TEXT EXTRACTION (PDF/DOCX)
# =====================================================================

def _extract_text_from_pdf_stream(stream: BytesIO) -> str:
    if PdfReader is None:
        raise HTTPException(status_code=500, detail="PDF support not installed.")
    raw_bytes = stream.getvalue()
    try:
        reader = PdfReader(BytesIO(raw_bytes))
        texts: List[str] = []
        for page in reader.pages:
            try:
                page_text = page.extract_text() or ""
            except Exception:
                page_text = ""
            if page_text.strip():
                texts.append(page_text)
        combined = "\n".join(texts).strip()
        return combined or "(No text extracted.)"
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read PDF: {exc}")


def _extract_text_from_docx_stream(stream: BytesIO) -> str:
    if docx is None:
        raise HTTPException(status_code=500, detail="DOCX support not installed.")
    try:
        document = docx.Document(stream)
        paras = [p.text for p in document.paragraphs]
        combined = "\n".join(paras).strip()
        return combined or "(No text extracted.)"
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read DOCX: {exc}")


def extract_text_from_upload(file: UploadFile, data: bytes) -> str:
    filename = (file.filename or "").lower()

    if filename.endswith(".pdf"):
        return _extract_text_from_pdf_stream(BytesIO(data))
    if filename.endswith(".docx") or filename.endswith(".doc"):
        return _extract_text_from_docx_stream(BytesIO(data))
    if filename.endswith(".txt"):
        try:
            return data.decode("utf-8", errors="ignore")
        except Exception as exc:
            raise HTTPException(
                status_code=400, detail=f"Failed to decode text file: {exc}"
            )
    return data.decode("utf-8", errors="ignore")


# =====================================================================
# SIMPLE LLM CLIENT
# =====================================================================

async def call_llm_for_review(req: AnalyzeRequestModel) -> str:
    """
    Use your existing LLMConfig + SYSTEM_PROMPT_BASE to generate a summary string.
    """
    cfg = load_llm_config()
    user_payload = {
        "document_name": req.document_name,
        "text": req.text,
        "hits": [h.dict() for h in req.hits],
    }
    payload_str = json.dumps(user_payload, ensure_ascii=False)

    system_prompt = SYSTEM_PROMPT_BASE
    if req.prompt_override:
        system_prompt = SYSTEM_PROMPT_BASE + "\n\nADDITIONAL:\n" + req.prompt_override

    effective_model = req.model
    temperature = req.temperature or 0.2

    if cfg.provider == LLMProvider.LOCAL_OLLAMA:
        api_url = cfg.local_api_url or OLLAMA_API_URL
        model_name = effective_model or cfg.local_model or OLLAMA_MODEL
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": payload_str},
            ],
            "stream": False,
            "temperature": temperature,
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
            resp = await client.post(api_url, json=payload)
            try:
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise HTTPException(status_code=502, detail=f"LLM request failed: {exc}")
            data = resp.json()
            content = None
            if isinstance(data, dict):
                if isinstance(data.get("message"), dict):
                    content = data["message"].get("content")
                elif isinstance(data.get("response"), str):
                    content = data["response"]
            if not isinstance(content, str) or not content.strip():
                raise HTTPException(status_code=502, detail="No assistant content.")
            return content.strip()

    # Remote HTTP omitted for brevity; you can add similar logic here if needed.
    raise HTTPException(status_code=500, detail="Unsupported LLM provider for analyze.")


async def call_llm_for_question(
    question: str,
    similar_bank_entries: List[QuestionBankEntryModel],
) -> str:
    """
    LLM call for a single questionnaire question, using QUESTIONNAIRE_SYSTEM_PROMPT.
    """
    cfg = load_llm_config()
    user_payload = {
        "question": question,
        "question_bank_entries": [
            {
                "id": e.id,
                "text": e.text,
                "answer": e.answer,
                "primary_tag": e.primary_tag,
                "frameworks": e.frameworks or [],
                "status": e.status,
            }
            for e in similar_bank_entries[:5]
        ],
    }
    payload_str = json.dumps(user_payload, ensure_ascii=False)
    temperature = 0.2

    if cfg.provider == LLMProvider.LOCAL_OLLAMA:
        api_url = cfg.local_api_url or OLLAMA_API_URL
        model_name = cfg.local_model or OLLAMA_MODEL
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": QUESTIONNAIRE_SYSTEM_PROMPT},
                {"role": "user", "content": payload_str},
            ],
            "stream": False,
            "temperature": temperature,
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
            resp = await client.post(api_url, json=payload)
            try:
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"LLM request (questionnaire) failed: {exc}",
                )
            data = resp.json()
            content = None
            if isinstance(data, dict):
                if isinstance(data.get("message"), dict):
                    content = data["message"].get("content")
                elif isinstance(data.get("response"), str):
                    content = data["response"]
            if not isinstance(content, str) or not content.strip():
                raise HTTPException(
                    status_code=502,
                    detail="LLM returned empty questionnaire answer.",
                )
            return content.strip()

    raise HTTPException(
        status_code=500,
        detail="Unsupported LLM provider for questionnaire.",
    )
async def call_llm_for_questions_batch(
    questions: List[QuestionnaireQuestionModel],
    bank_entries: List[QuestionBankEntryModel],
) -> dict[str, dict]:
    """
    Batch all questionnaire questions into a SINGLE LLM call.

    Returns:
      { "q1": {"answer": "...", "confidence": 0.82}, ... }
    """
    cfg = load_llm_config()

    if cfg.provider != LLMProvider.LOCAL_OLLAMA:
        raise HTTPException(
            status_code=500,
            detail="Batch questionnaire LLM is only implemented for local_ollama.",
        )

    api_url = cfg.local_api_url or OLLAMA_API_URL
    model_name = cfg.local_model or OLLAMA_MODEL

    bank_index = {b.id: b for b in bank_entries}

    questions_payload: List[dict] = []

    for q in questions:
        ctx_answers: List[dict] = []

        # include bank answer if available
        if q.matched_bank_id and q.matched_bank_id in bank_index:
            be = bank_index[q.matched_bank_id]
            ctx_answers.append(
                {
                    "id": be.id,
                    "text": be.text,
                    "answer": be.answer,
                    "primary_tag": be.primary_tag,
                    "frameworks": be.frameworks or [],
                }
            )

        # build knowledge citations for this question
        ctx_docs = _build_context_for_question(q.question_text)
        q.knowledge_sources = [
            {
                "doc_id": d.get("doc_id"),
                "source": d.get("source"),
                "doc_type": d.get("doc_type"),
                "score": d.get("score"),
            }
            for d in ctx_docs
        ]


    user_payload = {
        "questions": questions_payload,
        "instruction": (
            "You are answering multiple security / SCRM questions in batch for a U.S. "
            "Government contractor. For each question, return an object with: "
            "\"id\", \"answer\", and \"confidence\" (0.0–1.0). "
            "Respond ONLY with JSON of the format:\n"
            "{ \"answers\": [ {\"id\":\"q1\",\"answer\":\"...\",\"confidence\":0.82}, ... ] }"
        ),
    }

    payload_str = json.dumps(user_payload, ensure_ascii=False)

    system_prompt = QUESTIONNAIRE_SYSTEM_PROMPT + """
You MUST respond with STRICT JSON of the form:
{ "answers": [ { "id": "...", "answer": "...", "confidence": 0.85 }, ... ] }
No markdown. No extra commentary.
""".strip()

    body = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": payload_str},
        ],
        "stream": False,
        "temperature": 0.2,
    }

    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
        try:
            resp = await client.post(api_url, json=body)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            print(f"[BATCH LLM] HTTP error: {exc}")
            raise HTTPException(
                status_code=502,
                detail=f"Batch LLM (questionnaire) failed: {exc}",
            )

        raw = resp.text.strip()

        try:
            data = resp.json()
        except Exception:
            lines = [ln for ln in raw.splitlines() if ln.strip()]
            try:
                data = json.loads(lines[-1])
            except Exception as exc:
                print(f"[BATCH LLM] JSON parse failed: {exc}")
                raise HTTPException(
                    status_code=502,
                    detail="Batch LLM JSON parse failed.",
                )

        # get content (Ollama's formats)
        if isinstance(data, dict):
            if isinstance(data.get("message"), dict):
                content = data["message"].get("content")
            elif isinstance(data.get("response"), str):
                content = data["response"]
            else:
                content = data
        else:
            content = None

        if isinstance(content, str):
            try:
                content_json = json.loads(content)
            except Exception as exc:
                print(f"[BATCH LLM] content JSON parse failed: {exc}")
                raise HTTPException(
                    status_code=502,
                    detail="Batch LLM returned invalid JSON (content).",
                )
        elif isinstance(content, dict):
            content_json = content
        else:
            print("[BATCH LLM] invalid content:", content)
            raise HTTPException(
                status_code=502,
                detail="Batch LLM returned empty content.",
            )

        answers = content_json.get("answers")
        if not isinstance(answers, list):
            print("[BATCH LLM] 'answers' missing:", content_json)
            raise HTTPException(
                status_code=502,
                detail="Batch LLM JSON missing 'answers' list.",
            )

        result: dict[str, dict] = {}
        for item in answers:
            if not isinstance(item, dict):
                continue
            qid = item.get("id")
            ans = item.get("answer")
            conf = item.get("confidence", 0.6)
            if not isinstance(qid, str) or not isinstance(ans, str):
                continue
            try:
                conf_val = max(0.0, min(float(conf), 1.0))
            except Exception:
                conf_val = 0.6
            result[qid] = {"answer": ans.strip(), "confidence": conf_val}

        print("[BATCH LLM] answered questions:", list(result.keys()))
        return result

    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
        try:
            resp = await client.post(api_url, json=body)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            print(f"[BATCH LLM] HTTP error: {exc}")
            raise HTTPException(
                status_code=502,
                detail=f"Batch LLM (questionnaire) failed: {exc}",
            )

        raw = resp.text.strip()

        try:
            data = resp.json()
        except Exception:
            lines = [ln for ln in raw.splitlines() if ln.strip()]
            try:
                data = json.loads(lines[-1])
            except Exception as exc:
                print(f"[BATCH LLM] JSON parse failed: {exc}")
                raise HTTPException(
                    status_code=502,
                    detail="Batch LLM JSON parse failed.",
                )

        # get content (Ollama's weird formats)
        if isinstance(data, dict):
            if isinstance(data.get("message"), dict):
                content = data["message"].get("content")
            elif isinstance(data.get("response"), str):
                content = data["response"]
            else:
                content = data
        else:
            content = None

        if isinstance(content, str):
            try:
                content_json = json.loads(content)
            except Exception as exc:
                print(f"[BATCH LLM] content JSON parse failed: {exc}")
                raise HTTPException(
                    status_code=502,
                    detail="Batch LLM returned invalid JSON (content).",
                )
        elif isinstance(content, dict):
            content_json = content
        else:
            print("[BATCH LLM] invalid content:", content)
            raise HTTPException(
                status_code=502,
                detail="Batch LLM returned empty content.",
            )

        answers = content_json.get("answers")
        if not isinstance(answers, list):
            print("[BATCH LLM] 'answers' missing:", content_json)
            raise HTTPException(
                status_code=502,
                detail="Batch LLM JSON missing 'answers' list.",
            )

        result: dict[str, dict] = {}
        for item in answers:
            if not isinstance(item, dict):
                continue
            qid = item.get("id")
            ans = item.get("answer")
            conf = item.get("confidence", 0.6)
            if not isinstance(qid, str) or not isinstance(ans, str):
                continue
            try:
                conf_val = max(0.0, min(float(conf), 1.0))
            except Exception:
                conf_val = 0.6
            result[qid] = {"answer": ans.strip(), "confidence": conf_val}

        print("[BATCH LLM] answered questions:", list(result.keys()))
        return result


    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
        try:
            resp = await client.post(api_url, json=body)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            # Surface this clearly so we can see it in logs AND fall back
            print(f"[BATCH LLM] HTTP error: {exc}")
            raise HTTPException(
                status_code=502,
                detail=f"Batch LLM (questionnaire) failed: {exc}",
            )

        raw = resp.text.strip()

        try:
            data = resp.json()
        except Exception:
            # Some Ollama builds stream JSONL; fallback to last non-empty line
            lines = [ln for ln in raw.splitlines() if ln.strip()]
            try:
                data = json.loads(lines[-1])
            except Exception as exc:
                print(f"[BATCH LLM] JSONL parse failed: {exc}")
                raise HTTPException(
                    status_code=502,
                    detail=f"Batch LLM JSON parse failed: {exc}",
                )

        content = None
        if isinstance(data, dict):
            # (Ollama style) content may be in message/content or response
            if isinstance(data.get("message"), dict):
                content = data["message"].get("content")
            elif isinstance(data.get("response"), str):
                content = data["response"]
            else:
                content = data

        if isinstance(content, str):
            try:
                content_json = json.loads(content)
            except Exception as exc:
                print(f"[BATCH LLM] content JSON parse failed: {exc}")
                raise HTTPException(
                    status_code=502,
                    detail="Batch LLM content was not valid JSON.",
                )
        elif isinstance(content, dict):
            content_json = content
        else:
            print("[BATCH LLM] content is empty or invalid:", content)
            raise HTTPException(
                status_code=502,
                detail="Batch LLM returned empty or invalid content.",
            )

        answers = content_json.get("answers")
        if not isinstance(answers, list):
            print("[BATCH LLM] 'answers' key missing or not a list:", content_json)
            raise HTTPException(
                status_code=502,
                detail="Batch LLM JSON missing 'answers' list.",
            )

        result: dict[str, dict] = {}
        for item in answers:
            if not isinstance(item, dict):
                continue
            qid = item.get("id")
            ans = item.get("answer")
            conf = item.get("confidence", 0.6)
            if not isinstance(qid, str) or not isinstance(ans, str):
                continue
            try:
                conf_val = float(conf)
            except Exception:
                conf_val = 0.6
            conf_val = max(0.0, min(conf_val, 1.0))
            result[qid] = {"answer": ans.strip(), "confidence": conf_val}

        # Log what we got back
        print("[BATCH LLM] answered questions:", list(result.keys()))
        return result

#             return result

    # You can later extend this for REMOTE_HTTP if needed.
    raise HTTPException(
        status_code=500,
        detail="Batch LLM for questionnaire is only implemented for local_ollama.",
    )

# =====================================================================
# QUESTIONNAIRE HELPERS (bank + parsing + scoring)
# =====================================================================

def load_question_bank() -> List[QuestionBankEntryModel]:
    if not os.path.exists(QUESTION_BANK_PATH):
        return []
    try:
        with open(QUESTION_BANK_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    out: List[QuestionBankEntryModel] = []
    for item in data:
        try:
            out.append(
                QuestionBankEntryModel(
                    id=item.get("id"),
                    text=item.get("text", ""),
                    answer=item.get("answer", ""),
                    primary_tag=item.get("primary_tag"),
                    frameworks=item.get("frameworks") or [],
                    status=item.get("status") or "approved",
                )
            )
        except Exception:
            continue
    return out


def save_question_bank(entries: List[QuestionBankEntryModel]) -> None:
    serializable = [
        {
            "id": e.id,
            "text": e.text,
            "answer": e.answer,
            "primary_tag": e.primary_tag,
            "frameworks": e.frameworks,
            "status": e.status,
        }
        for e in entries
    ]
    with open(QUESTION_BANK_PATH, "w", encoding="utf-8") as f:
        json.dump(serializable, f, indent=2)


QUESTION_SPLIT_REGEX = re.compile(
    r"(?:^|\n)\s*(\d{1,3}\.|\-\s|\•\s)(.+?)(?=(?:\n\s*\d{1,3}\.)|\n\s*[-•]\s|\Z)",
    re.DOTALL,
)


def parse_questions_from_text(raw_text: str) -> List[QuestionnaireQuestionModel]:
    raw = raw_text.strip()
    if not raw:
        return []

    matches = QUESTION_SPLIT_REGEX.findall(raw)
    questions: List[QuestionnaireQuestionModel] = []

    if matches:
        for idx, (_, body) in enumerate(matches, start=1):
            text = body.strip().replace("\r", "")
            if not text:
                continue
            questions.append(
                QuestionnaireQuestionModel(
                    id=f"q{idx}",
                    question_text=text,
                    tags=[],
                    status="low_confidence",
                )
            )
    else:
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        for idx, line in enumerate(lines, start=1):
            questions.append(
                QuestionnaireQuestionModel(
                    id=f"q{idx}", question_text=line, tags=[], status="low_confidence"
                )
            )

    return questions


def _question_similarity(a: str, b: str) -> float:
    a_tokens = set(a.lower().split())
    b_tokens = set(b.lower().split())
    if not a_tokens or not b_tokens:
        return 0.0
    overlap = len(a_tokens & b_tokens)
    return overlap / max(1, len(a_tokens))


def derive_status_and_overall_confidence(
    questions: List[QuestionnaireQuestionModel],
) -> Optional[float]:
    confidences: List[float] = []
    for q in questions:
        c = q.confidence if q.confidence is not None else 0.0
        if q.answer_source == "bank":
            q.status = "auto_approved"
            if c == 0:
                c = 0.9
        else:
            if c >= 0.75:
                q.status = "needs_review"
            else:
                q.status = "low_confidence"
        q.confidence = c
        if c > 0:
            confidences.append(c)

    if not confidences:
        return None
    return sum(confidences) / len(confidences)
def _load_knowledge_docs_meta() -> List[KnowledgeDocMeta]:
    raw = _read_knowledge_store()
    docs: List[KnowledgeDocMeta] = []
    for item in raw:
        try:
            docs.append(KnowledgeDocMeta(**item))
        except Exception:
            continue
    return docs


def _load_knowledge_doc_text(doc_meta: KnowledgeDocMeta) -> str:
    path = os.path.join(KNOWLEDGE_DOCS_DIR, doc_meta.filename)
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _build_context_for_question(question_text: str, max_docs: int = 3) -> List[dict]:
    """
    Very simple retrieval: for now, we just compute a crude overlap between the
    question text and each knowledge doc's text, and pick the top few docs.

    Returns a list of { "source": title, "excerpt": "..."}.
    """
    docs = _load_knowledge_docs_meta()
    if not docs:
        return []

    q_tokens = set(question_text.lower().split())
    scored: List[tuple[float, KnowledgeDocMeta]] = []

    for meta in docs:
        text = _load_knowledge_doc_text(meta)
        if not text.strip():
            continue
        # basic overlap scoring
        doc_tokens = set(text.lower().split())
        if not doc_tokens:
            continue
        overlap = len(q_tokens & doc_tokens) / max(1, len(q_tokens))
        if overlap > 0:
            scored.append((overlap, meta))

    if not scored:
        return []

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:max_docs]

    results: List[dict] = []
    for score, meta in top:
        text = _load_knowledge_doc_text(meta)
        excerpt = text[:1000]  # keep it short; later we can find paragraphs
        results.append(
            {
                "source": meta.title,
                "doc_id": meta.id,
                "doc_type": meta.doc_type,
                "score": score,
                "excerpt": excerpt,
            }
        )
    return results


# =====================================================================
# ROUTES: FILES, /extract, /analyze
# =====================================================================

@app.get("/files/{filename}")
async def get_file(filename: str):
    file_path = os.path.join(FILES_DIR, filename)
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path, media_type="application/pdf")


@app.post("/extract", response_model=ExtractResponseModel)
async def extract(file: UploadFile = File(...)):
    filename = file.filename or ""
    ext = os.path.splitext(filename)[1].lower()
    try:
        contents = await file.read()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to read uploaded file: {exc}")
    if not contents:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    if ext == ".docx":
        text = _extract_text_from_docx_stream(BytesIO(contents))
        return ExtractResponseModel(text=text, type="docx")

    if ext == ".pdf":
        if PdfReader is None:
            raise HTTPException(status_code=500, detail="PDF support not installed.")
        safe_name = filename.replace(" ", "_") or "uploaded.pdf"
        pdf_path = os.path.join(FILES_DIR, safe_name)
        with open(pdf_path, "wb") as f:
            f.write(contents)
        pdf_url = f"/files/{safe_name}"
        text = _extract_text_from_pdf_stream(BytesIO(contents))
        return ExtractResponseModel(text=text, type="pdf", pdf_url=pdf_url, pages=None)

    # txt or other
    text = extract_text_from_upload(file, contents)
    return ExtractResponseModel(text=text, type=ext.lstrip(".") or "txt")


@app.post("/analyze", response_model=AnalyzeResponseModel)
async def analyze(req: AnalyzeRequestModel):
    """
    Simpler /analyze that just returns a single summary string and empty risks.
    """
    text = (req.text or "").strip()
    if not text or "no text extracted" in text.lower():
        summary = (
            "OBJECTIVE\n"
            "- The document appears to contain little or no machine-readable text.\n\n"
            "SCOPE\n"
            "- There is insufficient text to determine scope.\n\n"
            "KEY REQUIREMENTS\n"
            "- Requirements cannot be reliably extracted.\n\n"
            "KEY RISKS\n"
            "- Manual review is required.\n\n"
            "GAPS AND AMBIGUITIES\n"
            "- No machine-readable text is available.\n\n"
            "RECOMMENDED NEXT STEPS\n"
            "- Obtain a text-based version or perform manual review.\n"
        )
        return AnalyzeResponseModel(summary=summary, risks=[])

    try:
        summary_text = await call_llm_for_review(req)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"LLM error: {exc}")

    return AnalyzeResponseModel(summary=summary_text, risks=[])


# =====================================================================
# LLM CONFIG ROUTES (Settings page)
# =====================================================================

@app.get("/llm-config", response_model=LLMConfig)
async def get_llm_config_route():
    return load_llm_config()


@app.put("/llm-config", response_model=LLMConfig)
async def update_llm_config_route(cfg: LLMConfig):
    if cfg.provider == LLMProvider.REMOTE_HTTP:
        if not cfg.remote_base_url:
            raise HTTPException(status_code=400, detail="remote_base_url required")
        if not cfg.remote_model:
            raise HTTPException(status_code=400, detail="remote_model required")
    save_llm_config(cfg)
    return cfg


# =====================================================================
# FLAGS ROUTES
# =====================================================================

@app.get("/flags", response_model=FlagsPayload)
async def get_flags():
    try:
        return load_flags()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to load flags: {exc}")


@app.put("/flags", response_model=FlagsPayload)
async def update_flags(payload: FlagsPayload):
    try:
        save_flags(payload)
        return payload
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save flags: {exc}")


@app.post("/flags/test")
async def test_flags(payload: dict):
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Payload must include non-empty 'text'.")

    if "flags" in payload and payload["flags"]:
        try:
            flags_payload = FlagsPayload(**payload["flags"])
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid 'flags' payload: {exc}")
    else:
        flags_payload = load_flags()

    hits: List[dict] = []

    def process_rule(rule: FlagRule, group_name: str):
        rule_id = rule.id
        label = rule.label
        severity = getattr(rule, "severity", "Medium") or "Medium"
        category = getattr(rule, "category", None)
        scope_hint = getattr(rule, "scopeHint", None)
        patterns = rule.patterns or []
        for pattern in patterns:
            try:
                regex = re.compile(pattern, flags=re.IGNORECASE)
                for match in regex.finditer(text):
                    start = match.start()
                    line_num = text[:start].count("\n") + 1
                    hits.append(
                        {
                            "id": rule_id,
                            "label": label,
                            "group": group_name,
                            "severity": severity,
                            "category": category,
                            "scopeHint": scope_hint,
                            "line": line_num,
                            "match": match.group(0),
                        }
                    )
            except re.error:
                idx = text.lower().find(pattern.lower())
                if idx != -1:
                    line_num = text[:idx].count("\n") + 1
                    hits.append(
                        {
                            "id": rule_id,
                            "label": label,
                            "group": group_name,
                            "severity": severity,
                            "category": category,
                            "scopeHint": scope_hint,
                            "line": line_num,
                            "match": pattern,
                        }
                    )

    for group_name in ("clause", "context"):
        rules = getattr(flags_payload, group_name, []) or []
        for rule in rules:
            if getattr(rule, "enabled", True) is False:
                continue
            process_rule(rule, group_name)

    return {"hits": hits}


# =====================================================================
# REVIEWS ROUTES
# =====================================================================

@app.get("/reviews")
async def list_reviews():
    return _read_reviews_file()


@app.post("/reviews")
async def upsert_review(review: dict):
    if "id" not in review:
        raise HTTPException(status_code=400, detail="Review payload must include 'id'.")
    reviews = _read_reviews_file()
    existing_index = next((i for i, r in enumerate(reviews) if r.get("id") == review["id"]), None)
    if existing_index is None:
        reviews.append(review)
    else:
        reviews[existing_index] = review
    _write_reviews_file(reviews)
    return review


@app.delete("/reviews/{review_id}")
async def delete_review(review_id: str):
    reviews = _read_reviews_file()
    new_reviews = [r for r in reviews if r.get("id") != review_id]
    _write_reviews_file(new_reviews)
    return {"ok": True}


# =====================================================================
# QUESTIONNAIRE ROUTES
# =====================================================================

@app.post("/questionnaire/analyze", response_model=QuestionnaireAnalysisResponse)
async def questionnaire_analyze_json(body: QuestionnaireAnalyzeRequest):
    """
    Questionnaire analysis with batch LLM + robust fallback.

    Behavior:
      1) Parse questions from raw_text.
      2) For each question:
         - Try to match a bank entry.
         - If strong match → use bank answer.
      3) If llm_enabled:
         - Try a SINGLE batch LLM call for all remaining questions.
         - If batch fails or misses any questions → fall back to per-question LLM calls.
      4) Derive status + overall_confidence on backend.
    """
    text = (body.raw_text or "").strip()
    if not text:
        raise HTTPException(
            status_code=400,
            detail="raw_text must be a non-empty string",
        )

    questions = parse_questions_from_text(text)
    if not questions:
        return QuestionnaireAnalysisResponse(
            raw_text=text,
            questions=[],
            overall_confidence=None,
        )

    bank_entries = load_question_bank()
    BANK_STRONG_THRESHOLD = 0.7
    BANK_WEAK_THRESHOLD = 0.4

    remaining_for_llm: List[QuestionnaireQuestionModel] = []

    # ---- Pass 1: bank matching ----
    for q in questions:
        best_entry: Optional[QuestionBankEntryModel] = None
        best_score = 0.0

        for entry in bank_entries:
            s = _question_similarity(q.question_text, entry.text)
            if s > best_score:
                best_score = s
                best_entry = entry

        if best_entry and best_score >= BANK_STRONG_THRESHOLD:
            # Strong bank match → bank answer
            q.suggested_answer = best_entry.answer
            q.answer_source = "bank"
            q.matched_bank_id = best_entry.id
            q.confidence = min(
                0.98, 0.8 + (best_score - BANK_STRONG_THRESHOLD) * 0.4
            )
        else:
            # Keep mild matches for context only
            if best_entry and best_score >= BANK_WEAK_THRESHOLD:
                q.matched_bank_id = best_entry.id

            if body.llm_enabled:
                remaining_for_llm.append(q)
            else:
                q.suggested_answer = None
                q.answer_source = None
                q.confidence = None

    # ---- Pass 2: Batch LLM attempt ----
    unanswered_for_fallback: List[QuestionnaireQuestionModel] = []

    if body.llm_enabled and remaining_for_llm:
        try:
            batch_answers = await call_llm_for_questions_batch(
                remaining_for_llm,
                bank_entries,
            )
            print(
                "[QUESTIONNAIRE] Batch LLM returned answers for:",
                list(batch_answers.keys()),
            )

            for q in remaining_for_llm:
                ans_info = batch_answers.get(q.id)
                if not ans_info:
                    unanswered_for_fallback.append(q)
                    continue
                q.suggested_answer = ans_info["answer"]
                q.answer_source = "llm"
                q.confidence = ans_info["confidence"]
        except HTTPException as exc:
            # Log and mark all for fallback
            print("[QUESTIONNAIRE] Batch LLM error, falling back:", exc.detail)
            unanswered_for_fallback = list(remaining_for_llm)

    # ---- Pass 3: Per-question fallback for anything not answered ----
    if body.llm_enabled and unanswered_for_fallback:
        print(
            "[QUESTIONNAIRE] Falling back to per-question LLM for IDs:",
            [q.id for q in unanswered_for_fallback],
        )
        for q in unanswered_for_fallback:
            # Build weak context again if available
            similar_for_context: List[QuestionBankEntryModel] = []
            if q.matched_bank_id:
                candidate = next(
                    (b for b in bank_entries if b.id == q.matched_bank_id), None
                )
                if candidate:
                    similar_for_context.append(candidate)

            try:
                answer = await call_llm_for_question(
                    q.question_text,
                    similar_for_context,
                )
            except HTTPException as exc:
                print(f"[QUESTIONNAIRE] Per-question LLM error for '{q.id}': {exc.detail}")
                answer = ""

            if answer.strip():
                q.suggested_answer = answer.strip()
                q.answer_source = "llm"
                # keep confidence modest, batch model gave us nothing for this one
                q.confidence = 0.5
            else:
                # Still no answer; leave as low_confidence with no suggested_answer
                if q.suggested_answer is None:
                    q.answer_source = None
                    q.confidence = None

    # ---- Final: derive status + overall confidence ----
    overall_confidence = derive_status_and_overall_confidence(questions)
    return QuestionnaireAnalysisResponse(
        raw_text=text,
        questions=questions,
        overall_confidence=overall_confidence,
    )


@app.get("/question-bank", response_model=List[QuestionBankEntryModel])
async def get_question_bank_route():
    return load_question_bank()


@app.post("/question-bank", response_model=QuestionBankEntryModel)
async def upsert_question_bank_route(entry: QuestionBankUpsertModel):
    bank = load_question_bank()

    if entry.id:
        for idx, existing in enumerate(bank):
            if existing.id == entry.id:
                updated = existing.copy(
                    update={
                        "text": entry.text,
                        "answer": entry.answer,
                        "primary_tag": entry.primaryTag,
                        "frameworks": entry.frameworks or [],
                        "status": entry.status or existing.status,
                    }
                )
                bank[idx] = updated
                save_question_bank(bank)
                return updated

    new_id = entry.id or f"bank-{len(bank) + 1}"
    new_entry = QuestionBankEntryModel(
        id=new_id,
        text=entry.text,
        answer=entry.answer,
        primary_tag=entry.primaryTag,
        frameworks=entry.frameworks or [],
        status=entry.status or "approved",
    )
    bank.append(new_entry)
    save_question_bank(bank)
    return new_entry


@app.post("/questionnaire/feedback")
async def questionnaire_feedback(payload: QuestionnaireFeedbackRequest):
    # For now, this just acknowledges; you can later log or adjust weights.
    return {"ok": True}


# =====================================================================
# KNOWLEDGE CENTER ROUTES
# =====================================================================

@app.get("/knowledge/docs", response_model=KnowledgeDocListResponse)
async def list_knowledge_docs():
    raw = _read_knowledge_store()
    docs: List[KnowledgeDocMeta] = []
    for item in raw:
        try:
            docs.append(KnowledgeDocMeta(**item))
        except Exception:
            continue
    return KnowledgeDocListResponse(docs=docs)


@app.get("/knowledge/docs/{doc_id}", response_model=KnowledgeDocMeta)
async def get_knowledge_doc(doc_id: str):
    raw = _read_knowledge_store()
    for item in raw:
        if item.get("id") == doc_id:
            try:
                return KnowledgeDocMeta(**item)
            except Exception:
                break
    raise HTTPException(status_code=404, detail="Knowledge document not found")


@app.post("/knowledge/docs", response_model=KnowledgeDocMeta)
async def upload_knowledge_doc(
    file: UploadFile = File(...),
):
    """
    Upload an SSP / prior questionnaire / policy document to the Knowledge Center.

    For now:
      - We extract plain text.
      - We store the entire text as a .txt file under knowledge_docs/.
      - We create a simple metadata entry in knowledge_store.json.
    """
    raw_store = _read_knowledge_store()

    try:
        data = await file.read()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to read uploaded file: {exc}")
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    # reuse existing extract helper
    text = extract_text_from_upload(file, data)

    new_id = _new_knowledge_doc_id(raw_store)
    safe_name = f"{new_id}.txt"
    path = os.path.join(KNOWLEDGE_DOCS_DIR, safe_name)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save knowledge doc: {exc}")

    meta = KnowledgeDocMeta(
        id=new_id,
        title=file.filename or new_id,
        filename=safe_name,
        doc_type=None,
        tags=[],
        created_at=datetime.now().isoformat(),
        size_bytes=len(text.encode("utf-8", errors="ignore")),
    )

    raw_store.append(meta.model_dump())
    _write_knowledge_store(raw_store)

    return meta

# =====================================================================
# DEV ENTRYPOINT
# =====================================================================

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )
