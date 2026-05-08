#!/usr/bin/env python
"""BankBuddy Phase 1 smoke test.

Run AFTER `docker compose up -d --build` from the repository root. Verifies:

  1. Public surfaces: ui (8080) and api (8000) /health respond.
  2. /config exposes api_base_url + auth_provider.
  3. Internal surfaces (agent:8100, mock-bank:8200, postgres:5432) are NOT
     reachable from the host.
  4. Local-dev auth round-trip: POST /auth/local-dev/exchange sets a session
     cookie, GET /me returns the principal.
  5. End-to-end chat: POST /chat with the session cookie returns an assistant
     reply (proves api -> agent -> mock-bank -> postgres).

Usage:
    python infra/scripts/smoke_test.py
    python infra/scripts/smoke_test.py --skip-chat   # if no LLM is running
"""
from __future__ import annotations

import argparse
import socket
import sys

import httpx

UI = "http://localhost:8080"
API = "http://localhost:8000"
INTERNAL_PORTS = [
    ("agent", 8100),
    ("mock-bank", 8200),
    ("postgres", 5432),
]


def _ok(msg: str) -> None:
    print(f"  [ok] {msg}")


def _fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")
    raise SystemExit(1)


def check_public_health() -> None:
    print("[1] public /health endpoints")
    for name, url in [("ui", f"{UI}/health"), ("api", f"{API}/health")]:
        r = httpx.get(url, timeout=5.0)
        if r.status_code != 200:
            _fail(f"{name} {url} -> {r.status_code}")
        _ok(f"{name} {url} -> {r.json()}")


def check_config() -> None:
    print("[2] /config")
    r = httpx.get(f"{UI}/config", timeout=5.0)
    if r.status_code != 200:
        _fail(f"/config -> {r.status_code}")
    cfg = r.json()
    if "api_base_url" not in cfg or "auth_provider" not in cfg:
        _fail(f"/config missing keys: {cfg}")
    _ok(f"config = {cfg}")


def check_internal_isolation() -> None:
    print("[3] internal ports must NOT be host-reachable")
    for name, port in INTERNAL_PORTS:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            try:
                s.connect(("localhost", port))
                _fail(f"{name}:{port} reachable from host - security boundary leak")
            except (ConnectionRefusedError, socket.timeout, OSError):
                _ok(f"{name}:{port} not reachable (good)")


def check_auth_roundtrip() -> str:
    print("[4] local-dev auth round-trip")
    with httpx.Client(base_url=API, timeout=5.0) as c:
        r = c.post("/auth/local-dev/exchange", json={"username": "alice"})
        if r.status_code != 200:
            _fail(f"exchange -> {r.status_code} {r.text}")
        cookie = r.cookies.get("bankbuddy_session")
        if not cookie:
            _fail("no bankbuddy_session cookie set")
        _ok(f"exchange ok, cookie len={len(cookie)}")

        r = c.get("/me")
        if r.status_code != 200:
            _fail(f"/me -> {r.status_code} {r.text}")
        me = r.json()
        if me.get("username") != "alice":
            _fail(f"/me unexpected: {me}")
        _ok(f"/me = {me}")
        return cookie


def check_chat(cookie: str) -> None:
    print("[5] end-to-end chat (api -> agent -> mock-bank)")
    with httpx.Client(base_url=API, timeout=60.0, cookies={"bankbuddy_session": cookie}) as c:
        r = c.post("/chat", json={"message": "What are my account balances?"})
        if r.status_code != 200:
            _fail(f"/chat -> {r.status_code} {r.text}")
        data = r.json()
        if not data.get("reply"):
            _fail(f"/chat no reply: {data}")
        _ok(f"reply ({len(data['reply'])} chars): {data['reply'][:140]}...")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-chat", action="store_true",
                        help="Skip the LLM-dependent chat round-trip.")
    args = parser.parse_args()

    check_public_health()
    check_config()
    check_internal_isolation()
    cookie = check_auth_roundtrip()
    if args.skip_chat:
        print("[5] chat round-trip skipped (--skip-chat)")
    else:
        check_chat(cookie)

    print("\nALL SMOKE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
