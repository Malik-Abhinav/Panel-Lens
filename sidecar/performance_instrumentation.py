"""Opt-in structured performance events for the PanelLens pipeline.

Events deliberately contain identifiers, timings, dimensions, counts, and model
metadata only. Callers must never attach source images, OCR text, or translations.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator


PERF_ENV = "PANELLENS_PERF_ENABLED"
PERF_LOG_ENV = "PANELLENS_PERF_LOG"


def _environment_enabled() -> bool:
    return os.environ.get(PERF_ENV, "").casefold() in {"1", "true", "yes", "on"}


@dataclass
class RequestContext:
    request_id: str
    image_id: str
    priority: str = "visible"
    model_state: str = "unknown"
    metrics: dict[str, Any] = field(default_factory=dict)


_context: contextvars.ContextVar[RequestContext | None] = contextvars.ContextVar(
    "panellens_performance_context", default=None
)


class PerformanceRecorder:
    """Thread-safe JSONL recorder with an injectable sink for tests/benchmarks."""

    def __init__(
        self,
        enabled: bool | None = None,
        sink: Callable[[dict[str, Any]], None] | None = None,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self.enabled = _environment_enabled() if enabled is None else enabled
        self._sink = sink
        self._clock_ns = clock_ns
        self._lock = threading.Lock()

    def now_ns(self) -> int:
        return self._clock_ns()

    def emit(self, stage: str, **fields: Any) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        context = _context.get()
        event: dict[str, Any] = {
            "schema_version": 1,
            "timestamp_ns": self.now_ns(),
            "stage": stage,
            "request_id": fields.pop(
                "request_id", context.request_id if context else ""
            ),
            "image_id": fields.pop(
                "image_id", context.image_id if context else ""
            ),
            "priority": fields.pop(
                "priority", context.priority if context else "unknown"
            ),
            "cache_level": fields.pop("cache_level", "none"),
            "model_state": fields.pop(
                "model_state", context.model_state if context else "unknown"
            ),
        }
        event.update({key: value for key, value in fields.items() if value is not None})
        with self._lock:
            if self._sink is not None:
                self._sink(event)
            else:
                self._write_jsonl(event)
        return event

    def _write_jsonl(self, event: dict[str, Any]) -> None:
        encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        path = os.environ.get(PERF_LOG_ENV, "").strip()
        if not path:
            sys.stderr.write(f"[PanelLens performance] {encoded}\n")
            sys.stderr.flush()
            return
        destination = Path(path).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("a", encoding="utf-8") as stream:
            stream.write(encoded + "\n")

    @contextlib.contextmanager
    def span(self, stage: str, **fields: Any) -> Iterator[None]:
        started = self.now_ns()
        self.emit(f"{stage}_start", **fields)
        try:
            yield
        finally:
            self.emit(
                f"{stage}_end",
                duration_ms=round((self.now_ns() - started) / 1_000_000, 3),
                **fields,
            )


recorder = PerformanceRecorder()


@contextlib.contextmanager
def bind_request(
    request_id: Any,
    image_id: Any,
    priority: Any = "visible",
    model_state: str = "unknown",
) -> Iterator[RequestContext]:
    context = RequestContext(
        request_id=str(request_id or ""),
        image_id=str(image_id or request_id or ""),
        priority=str(priority or "visible"),
        model_state=model_state,
    )
    token = _context.set(context)
    try:
        yield context
    finally:
        _context.reset(token)


def emit(stage: str, **fields: Any) -> dict[str, Any] | None:
    return recorder.emit(stage, **fields)


def current_context() -> RequestContext | None:
    return _context.get()


def set_model_state(state: str) -> None:
    context = _context.get()
    if context is not None:
        context.model_state = state


def add_metrics(**metrics: Any) -> None:
    context = _context.get()
    if context is not None:
        context.metrics.update(
            {key: value for key, value in metrics.items() if value is not None}
        )
