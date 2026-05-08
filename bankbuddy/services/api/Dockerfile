FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1

RUN groupadd --system app && useradd --system --gid app --home /app app

WORKDIR /app

COPY shared /app/shared
RUN pip install -e /app/shared

COPY services/api/requirements.txt /app/services/api/requirements.txt
RUN pip install -r /app/services/api/requirements.txt

COPY services/api /app/services/api

USER app
WORKDIR /app/services/api

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
