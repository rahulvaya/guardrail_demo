"""Telemetry interface (traces, metrics, logs).

Implementations:
    - OTelTelemetry  (default; OpenTelemetry SDK -> OTLP)
    - NoopTelemetry  (tests)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from typing import Any


class ITelemetry(ABC):
    @abstractmethod
    def start_span(self, name: str, **attributes: Any) -> AbstractContextManager[Any]: ...

    @abstractmethod
    def log_event(self, name: str, **attributes: Any) -> None: ...

    @abstractmethod
    def record_metric(self, name: str, value: float, **attributes: Any) -> None: ...
