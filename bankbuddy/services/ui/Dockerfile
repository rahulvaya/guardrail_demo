# Multi-stage build: 1) build React with Node, 2) serve via FastAPI/uvicorn.

# ---- Stage 1: build React (Vite) ----
FROM node:20-alpine AS web-build
WORKDIR /web
COPY services/ui/web/package.json services/ui/web/package-lock.json* ./
RUN npm install
COPY services/ui/web ./
RUN npm run build

# ---- Stage 2: FastAPI backend that serves the static React build ----
FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
RUN groupadd --system app && useradd --system --gid app --home /app app

WORKDIR /app

COPY shared /app/shared
RUN pip install -e /app/shared

COPY services/ui/requirements.txt /app/services/ui/requirements.txt
RUN pip install -r /app/services/ui/requirements.txt

COPY services/ui /app/services/ui
COPY --from=web-build /web/dist /app/services/ui/static

USER app
WORKDIR /app/services/ui

EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
