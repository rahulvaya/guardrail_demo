"""Session store interface.

Implementations:
    - PostgresSessionStore  (default)
    - InMemorySessionStore  (tests)
    - RedisSessionStore     (future)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ISessionStore(ABC):
    @abstractmethod
    async def get(self, session_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    async def set(self, session_id: str, data: dict[str, Any], *, ttl_seconds: int | None = None) -> None: ...

    @abstractmethod
    async def delete(self, session_id: str) -> None: ...
