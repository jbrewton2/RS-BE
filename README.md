
---
# Contract Security Studio — Backend API

This repository contains the **FastAPI backend** for Contract Security Studio (CSS).

It provides:
- Contract analysis APIs
- Questionnaire ingestion & analysis
- Knowledge base management
- Heuristic flagging
- LLM configuration, pricing, and telemetry
- JWT-protected API surface

---

## 🧱 Tech Stack

- Python 3.11+
- FastAPI
- Pydantic
- Keycloak (OIDC)
- Ollama (local LLM)
- JSON-based persistence (file-backed)

---

## 🔐 Authentication & Security

### Auth Model
- JWTs issued by Keycloak
- Verified via JWKS
- Enforced at router level
- Public endpoints are explicitly limited

### Public Endpoints
- `GET /health`
- `GET /health/llm`

### Protected Endpoints
All others, including:
- `/reviews`
- `/flags`
- `/knowledge`
- `/questionnaire`
- `/questionnaires`
- `/llm-config`
- `/llm-pricing`
- `/llm-status`

Unauthorized access returns `401`.

---

## 🧠 LLM Integration

- Default provider: **Ollama**
- Default model: `llama3.1:8b-instruct-q4_K_M`
- Supports future remote providers

### Telemetry
- LLM usage is logged to `llm_stats.json`
- Aggregated via `/llm-status`
- Cost derived from pricing config

---

## 📁 Repo Structure

```text
backend/
├── main.py
├── auth/
│   └── jwt.py
├── reviews/
├── flags/
├── knowledge/
├── questionnaire/
├── llm_config/
├── pricing/
├── llm_status/
└── health/

🚀 Running Locally

Normally run via css-infra.

Standalone (dev only):

uvicorn backend.main:app --reload

🧪 API Docs

Once running:

http://localhost:8000/docs

📌 Design Notes

Router-level auth avoids accidental exposure

File-backed persistence keeps system simple & inspectable

LLM usage is auditable and cost-aware

Backend is ready for RBAC expansion

🚧 Future Enhancements

Role-based access control (admin vs reviewer)

Database persistence (optional)

External LLM providers

Metrics export (Prometheus/OpenTelemetry)