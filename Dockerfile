FROM python:3.11-slim

WORKDIR /app

# Minimal OS deps (safe for common python wheels)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
 && rm -rf /var/lib/apt/lists/*

# Install deps first for better layer caching
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# IMPORTANT: copy your backend source into /app/backend so "backend.*" imports work
COPY . /app/backend

EXPOSE 8000

# Run FastAPI as a package module
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
