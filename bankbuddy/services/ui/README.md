# services/ui

User-facing web app: **FastAPI backend + React (Vite) frontend** packaged into a single container.

## Status

**Phase 1a:** placeholder. The React app renders a status page and reads `/config` at runtime to confirm wiring. Real login + chat UI arrive in Phase 1e.

## Layout

```text
services/ui/
  Dockerfile           # multi-stage: node build -> python serve
  requirements.txt     # FastAPI / uvicorn (server-side)
  app/
    main.py            # FastAPI app; serves /config and the SPA
    static/            # populated at build time from web/dist (in container)
  web/
    package.json       # React + Vite
    vite.config.js
    index.html
    src/
      main.jsx
      App.jsx
      styles.css
```

## Why FastAPI in the UI container?

- Serves the React build with SPA routing fallback.
- Exposes `/config` at runtime so the **same image** works in any environment - no rebuild to change `PUBLIC_API_BASE_URL`.
- Centralizes future BFF concerns (e.g., setting Secure cookies for auth callbacks) inside the same trust boundary as the SPA.

## Network

- Edge network only (published on host port `${UI_PORT}`, default 8080).
- Talks **only** to the `api` service. Never to `agent` or `mock-bank`.

## Design principles

| Principle | Where |
|-----------|-------|
| Single Responsibility | Presentation only; no business logic |
| Twelve-Factor (config) | Runtime `/config` endpoint, no env baked into the JS bundle |
| Layered architecture | UI -> API only |

## Configuration

| Env var | Purpose |
|---------|---------|
| `UI_PORT` | Host-published port |
| `PUBLIC_API_BASE_URL` | URL the browser uses to reach the API |
| `AUTH_PROVIDER` | Surfaced via `/config` so the SPA can show the right login button |

## Local dev (without Docker)

```powershell
# Terminal 1 - React with hot reload
cd services/ui/web
npm install
npm run dev          # http://localhost:5173

# Terminal 2 - FastAPI
cd services/ui
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
```

## Cloud portability

This container is fully cloud-agnostic - it's just static assets plus a tiny Python server. To put a CDN in front (CloudFront / Front Door / Cloud CDN), point it at this container's `/` and `/assets` paths.
