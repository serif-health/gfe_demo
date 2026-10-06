"""Lightweight trace event bus.

Every layer of the service (HTTP, FHIR parsing, SQL, pricing engine, builders) emits
structured events here. Sinks subscribe to render them (the Rich terminal console) or
to collect them (tests). Keeping this decoupled means the business code never imports
any presentation library.
"""

from __future__ import annotations

import contextvars
import itertools
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("gfe_request_id", default=None)
_counter = itertools.count(1)


@dataclass
class TraceEvent:
    kind: str
    data: dict[str, Any]
    request_id: str | None
    ts: float = field(default_factory=time.time)


Sink = Callable[[TraceEvent], None]


class Tracer:
    def __init__(self) -> None:
        self._sinks: list[Sink] = []

    def add_sink(self, sink: Sink) -> None:
        if sink not in self._sinks:
            self._sinks.append(sink)

    def remove_sink(self, sink: Sink) -> None:
        if sink in self._sinks:
            self._sinks.remove(sink)

    def emit(self, kind: str, **data: Any) -> None:
        if not self._sinks:
            return
        event = TraceEvent(kind=kind, data=data, request_id=_request_id.get())
        for sink in list(self._sinks):
            try:
                sink(event)
            except Exception:  # a broken renderer must never break request handling
                pass

    # Convenience helpers -------------------------------------------------
    def step(self, message: str, **data: Any) -> None:
        self.emit("engine.step", message=message, **data)

    def warn(self, message: str, **data: Any) -> None:
        self.emit("engine.warn", message=message, **data)


tracer = Tracer()


def new_request_id() -> str:
    return f"req-{next(_counter):04d}"


def set_request_id(request_id: str | None) -> contextvars.Token:
    return _request_id.set(request_id)


def reset_request_id(token: contextvars.Token) -> None:
    _request_id.reset(token)


def current_request_id() -> str | None:
    return _request_id.get()
