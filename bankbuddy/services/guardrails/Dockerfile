FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1

RUN groupadd --system app && useradd --system --gid app --home /app app

WORKDIR /app

COPY shared /app/shared
RUN pip install -e /app/shared

COPY services/guardrails/requirements.txt /app/services/guardrails/requirements.txt
RUN pip install -r /app/services/guardrails/requirements.txt

# The guardrails service owns the guard implementations (under app/core/)
# as well as the policy loader and HTTP layer.
COPY services/guardrails /app/services/guardrails

USER app
WORKDIR /app/services/guardrails

EXPOSE 8001
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
