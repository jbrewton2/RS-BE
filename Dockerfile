FROM python:3.11-slim

# Prevent python from writing pyc files & buffering stdout
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# System deps:
# - libreoffice-writer: DOCX -> PDF conversion via soffice
# - fonts: avoids blank/missing glyph PDFs
# - tini: better PID 1 behavior (optional but nice)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    tini \
    libreoffice-writer \
    libreoffice-core \
    libreoffice-common \
    fonts-dejavu-core \
    fonts-liberation \
 && rm -rf /var/lib/apt/lists/*

# Install Python deps first (better caching)
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy repo
COPY . /app

EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--"]

# Run FastAPI from repo root module
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
