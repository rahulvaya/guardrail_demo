"""HTTP routes for the API gateway."""
from __future__ import annotations

import logging
import secrets
import uuid

from bankbuddy_shared.contracts.agent import AgentInvokeRequest
from bankbuddy_shared.contracts.chat import ChatMessage, ChatRequest, ChatResponse, MessageRole
from bankbuddy_shared.contracts.principal import Principal
from bankbuddy_shared.interfaces.auth import AuthError
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from .session import decode_session_jwt, issue_session_jwt
from .settings import get_settings

log = logging.getLogger("api.routers")
router = APIRouter()


# ---------- auth ----------


class LocalLoginIn(BaseModel):
    username: str  # "alice" | "bob"


class LoginUrlOut(BaseModel):
    login_url: str
    state: str


class MeOut(BaseModel):
    subject: str
    username: str | None
    email: str | None
    tenant_id: str | None
    roles: list[str]


@router.get("/auth/login-url", response_model=LoginUrlOut)
def login_url(request: Request) -> LoginUrlOut:
    state = secrets.token_urlsafe(24)
    url = request.app.state.auth.get_login_url(state)
    return LoginUrlOut(login_url=url, state=state)


@router.post("/auth/local-dev/exchange")
async def local_dev_exchange(body: LocalLoginIn, response: Response, request: Request) -> MeOut:
    settings = get_settings()
    if settings.auth_provider != "local-dev":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "local-dev exchange disabled")
    try:
        principal: Principal = await request.app.state.auth.exchange_code(body.username)
    except AuthError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e)) from e
    token = issue_session_jwt(settings, principal)
    response.set_cookie(
        key="bankbuddy_session",
        value=token,
        max_age=settings.app_jwt_ttl_seconds,
        httponly=True,
        samesite="lax",
        secure=False,  # localhost; flip to True behind TLS
    )
    return _principal_to_me(principal)


@router.post("/auth/logout")
def logout(response: Response) -> dict[str, str]:
    response.delete_cookie("bankbuddy_session")
    return {"status": "ok"}


def _principal_to_me(p: Principal) -> MeOut:
    return MeOut(
        subject=p.subject,
        username=p.username,
        email=p.email,
        tenant_id=p.tenant_id,
        roles=list(p.roles),
    )


def require_principal(
    bankbuddy_session: str | None = Cookie(default=None),
) -> Principal:
    settings = get_settings()
    if not bankbuddy_session:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "no session")
    try:
        return decode_session_jwt(settings, bankbuddy_session)
    except ValueError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e)) from e


@router.get("/me", response_model=MeOut)
def me(principal: Principal = Depends(require_principal)) -> MeOut:
    return _principal_to_me(principal)


# ---------- chat ----------


@router.post("/chat", response_model=ChatResponse)
async def chat(
    body: ChatRequest,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> ChatResponse:
    session_id = body.session_id or f"sess-{uuid.uuid4().hex[:12]}"
    invoke = AgentInvokeRequest(
        session_id=session_id,
        message=body.message,
        principal=principal,
    )
    try:
        agent_resp = await request.app.state.agent_client.invoke(invoke)
    except Exception as e:
        log.exception("agent call failed")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"agent error: {e}") from e

    return ChatResponse(
        session_id=session_id,
        reply=agent_resp.reply,
        messages=[
            ChatMessage(role=MessageRole.USER, content=body.message),
            ChatMessage(role=MessageRole.ASSISTANT, content=agent_resp.reply),
        ],
        trace={
            "guardrails": agent_resp.metadata.get("guardrails", {}),
            "blocked": agent_resp.metadata.get("blocked", False),
            "blocked_at": agent_resp.metadata.get("blocked_at"),
            "block_reasons": agent_resp.metadata.get("block_reasons", []),
            "block_categories": agent_resp.metadata.get("block_categories", []),
            "tool_calls": [tc.model_dump() for tc in agent_resp.tool_calls],
        },
    )
