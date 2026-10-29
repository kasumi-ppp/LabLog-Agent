"""Task handler registry.

A "task type" is a named function that receives (ctx, payload) and returns a
JSON-serializable result. The executor runs handlers in worker threads; the
handler reports progress through ctx and must check ctx.cancelled() at safe
points so cancellation stays cooperative (a handler cannot be killed mid-line).

Register new task types with @register("name") — nothing else needs to change.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable


class TaskCancelled(Exception):
    """Raised inside a handler when the user asked to cancel the task."""


class InvalidPayload(ValueError):
    """Raised when the payload does not satisfy the handler's contract."""


@dataclass
class HandlerContext:
    task_id: int
    _progress: Callable[[int, str], None]
    _cancelled: Callable[[], bool]
    _events: list = field(default_factory=list)

    def progress(self, percent: int, message: str = "") -> None:
        self._progress(percent, message)

    def cancelled(self) -> bool:
        return self._cancelled()

    def check_cancelled(self) -> None:
        if self._cancelled():
            raise TaskCancelled("cancelled by user request")


Handler = Callable[[HandlerContext, dict], Any]
_REGISTRY: dict[str, Handler] = {}


def register(name: str):
    def decorator(func: Handler) -> Handler:
        _REGISTRY[name] = func
        return func
    return decorator


def known_types() -> list[str]:
    return sorted(_REGISTRY)


def get_handler(name: str) -> Handler | None:
    return _REGISTRY.get(name)


# --------------------------------------------------------------------------
# Built-in demo handlers
# --------------------------------------------------------------------------

def _require_mapping(payload: dict) -> None:
    if not isinstance(payload, dict):
        raise InvalidPayload("payload must be a JSON object")


@register("echo")
def handler_echo(ctx: HandlerContext, payload: dict) -> dict:
    """Return the payload unchanged — the smallest possible lifecycle demo."""
    _require_mapping(payload)
    ctx.check_cancelled()
    ctx.progress(50, "echoing payload")
    ctx.check_cancelled()
    return {"echo": payload, "message": "payload returned unchanged"}


@register("word_count")
def handler_word_count(ctx: HandlerContext, payload: dict) -> dict:
    """Count characters/words/lines of payload['text']."""
    _require_mapping(payload)
    text = payload.get("text")
    if not isinstance(text, str):
        raise InvalidPayload("payload.text must be a string")
    ctx.check_cancelled()
    ctx.progress(60, "counting")
    return {
        "characters": len(text),
        "words": len([w for w in text.split() if w.strip()]),
        "lines": len(text.splitlines()) or (1 if text else 0),
    }


@register("sum_numbers")
def handler_sum_numbers(ctx: HandlerContext, payload: dict) -> dict:
    """Sum payload['numbers']; bad input raises -> task ends failed."""
    _require_mapping(payload)
    numbers = payload.get("numbers")
    if not isinstance(numbers, list) or not numbers:
        raise InvalidPayload("payload.numbers must be a non-empty list")
    cleaned: list[float] = []
    for index, value in enumerate(numbers):
        ctx.check_cancelled()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InvalidPayload(f"payload.numbers[{index}] is not a number: {value!r}")
        cleaned.append(float(value))
        ctx.progress(int(90 * len(cleaned) / len(numbers)), f"absorbed {len(cleaned)}/{len(numbers)}")
    total = sum(cleaned)
    return {"count": len(cleaned), "sum": int(total) if total.is_integer() else total,
            "mean": round(total / len(cleaned), 4)}


@register("simulate_work")
def handler_simulate_work(ctx: HandlerContext, payload: dict) -> dict:
    """Multi-step job with real progress and optional failure — exercises the
    full lifecycle: running -> progress -> (failed | cancelled | succeeded).

    payload: {"steps": 5, "step_seconds": 0.5, "fail_at": 3}
      fail_at: 1-based step number that raises; 0/absent never fails.
    """
    _require_mapping(payload)
    steps = payload.get("steps", 5)
    step_seconds = payload.get("step_seconds", 0.5)
    fail_at = payload.get("fail_at", 0)
    if not isinstance(steps, int) or not 1 <= steps <= 100:
        raise InvalidPayload("payload.steps must be an integer in [1, 100]")
    if not isinstance(step_seconds, (int, float)) or not 0 <= step_seconds <= 10:
        raise InvalidPayload("payload.step_seconds must be a number in [0, 10]")
    if not isinstance(fail_at, int) or not 0 <= fail_at <= steps:
        raise InvalidPayload("payload.fail_at must be an integer in [0, steps]")

    for step in range(1, steps + 1):
        ctx.check_cancelled()
        ctx.progress(int(100 * (step - 1) / steps), f"step {step}/{steps} in progress")
        time.sleep(float(step_seconds))
        if fail_at and step == fail_at:
            raise RuntimeError(f"simulated failure at step {fail_at}/{steps}")
        ctx.progress(int(100 * step / steps), f"step {step}/{steps} done")
    return {"steps_done": steps, "seconds_slept": round(steps * step_seconds, 3),
            "message": "all steps completed"}
