FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1

RUN groupadd --system app && useradd --system --gid app --home /app app

WORKDIR /app

# Install shared package first (changes rarely)
COPY shared /app/shared
RUN pip install -e /app/shared

# Service-specific deps
COPY services/mock-bank/requirements.txt /app/services/mock-bank/requirements.txt
RUN pip install -r /app/services/mock-bank/requirements.txt

# App code
COPY services/mock-bank /app/services/mock-bank

USER app
WORKDIR /app/services/mock-bank

EXPOSE 8200
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8200"]
