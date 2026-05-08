FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1

RUN groupadd --system app && useradd --system --gid app --home /app app

WORKDIR /app

COPY shared /app/shared
RUN pip install -e /app/shared

COPY services/agent/requirements.txt /app/services/agent/requirements.txt
RUN pip install -r /app/services/agent/requirements.txt

COPY services/agent /app/services/agent

USER app
WORKDIR /app/services/agent

EXPOSE 8100
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100"]
