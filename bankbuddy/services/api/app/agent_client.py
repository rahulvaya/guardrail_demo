"""Internal client to the agent service.

The api service is the only thing on the `internal` network allowed to
hold `AGENT_INTERNAL_TOKEN`. The browser never sees it.
"""
from __future__ import annotations

import httpx

from bankbuddy_shared.contracts.agent import AgentInvokeRequest, AgentInvokeResponse


class AgentClient:
    def __init__(self, base_url: str, internal_token: str, timeout: float = 180.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = internal_token
        self._timeout = timeout

    async def invoke(self, request: AgentInvokeRequest) -> AgentInvokeResponse:
        async with httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout) as client:
            resp = await client.post(
                "/internal/invoke",
                json=request.model_dump(mode="json"),
                headers={"X-Internal-Token": self._token},
            )
            resp.raise_for_status()
            return AgentInvokeResponse.model_validate(resp.json())
