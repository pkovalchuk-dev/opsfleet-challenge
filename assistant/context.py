"""Trusted runtime context; prompt builders copy selected informational fields."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import Limits, User


@dataclass
class RuntimeContext:
    user: User
    conversation_id: str
    turn_id: str
    limits: Limits
    llm: Any
    bq: Any
    store: Any
    tracer: Any
    schema_doc: str
    settings: dict
    started_at: float = field(default_factory=time.monotonic)

    client_factory: Callable | None = None
    displayed_steps: int = 0  # Completed actions already shown before a confirmation pause.

    def ensure_clients(self) -> None:
        # A resumed deletion stays local; later model work initializes clients only when reached.
        if self.client_factory and (self.llm is None or self.bq is None):
            self.llm, self.bq = self.client_factory()

    @property
    def deadline(self) -> float:
        return self.started_at + self.limits.timeout_seconds

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def expired(self) -> bool:
        return self.remaining() <= 0.0
