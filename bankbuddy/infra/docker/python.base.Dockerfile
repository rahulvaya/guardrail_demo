# Common base for all Python services in BankBuddy.
# Each service can extend this or copy the relevant lines into its own Dockerfile.
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Non-root user
RUN groupadd --system app && useradd --system --gid app --home /app app

WORKDIR /app
