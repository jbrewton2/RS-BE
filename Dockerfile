FROM python:3.11-slim

# Prevent python from writing pyc files & buffering stdout
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# System deps (minimal, safe)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
 && rm -rf /var/lib/apt/lists/*

# Install Python deps first (better caching)
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# ✅ Copy repo as-is into /app (no fake /app/backend package)
COPY . /app

EXPOSE 8000

# ✅ Run FastAPI from repo root module
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
