"""Agent trace: a step-by-step record of what each agent received, asked the model, got back and proposed.

Recorded while the system runs, without changing any agent:
- TracedAgent wraps an agent: inputs, output, proposed actions, time.
- TracingLLM wraps the model: the exact prompt, the raw answer, model, time and cost.
- The gateway and orchestrator add policy decisions, executions and stage changes.
Every step is tagged with the event it belongs to, so a trace shows one event end to end.
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, Field

from revenue_agents.models import Model
from revenue_agents.storage import STATE_DIR, append_jsonl, read_jsonl, utc_now

T = TypeVar("T", bound=BaseModel)

_current_event: ContextVar[str] = ContextVar("current_event", default="manual")


class Span(Model):
    event_id: str
    at: dt.datetime
    kind: str  # "run" (an agent run starts), "tool", "agent" (it ends), "policy", "action", "stage" or "llm"
    name: str
    duration_ms: float | None = None
    data: dict[str, Any] = Field(default_factory=dict)


# Called for every step as it happens: (kind, name, data, duration_ms). Used for live progress in the UI.
Listener = Callable[[str, str, dict[str, Any], float | None], None]


class Tracer:
    def __init__(self, path: Path = STATE_DIR / "traces.jsonl") -> None:
        self._path = Path(path)
        self._listeners: list[Listener] = []
        self._cache: tuple[tuple[int, int], list[Span]] | None = None

    @contextmanager
    def listening(self, listener: Listener) -> Iterator[None]:
        """Receive every step live while the block runs (e.g. to draw progress in the UI)."""
        self._listeners.append(listener)
        try:
            yield
        finally:
            self._listeners.remove(listener)

    def notify(self, kind: str, name: str, data: dict[str, Any]) -> None:
        """A live-only signal, such as "agent started". Not saved in the trace."""
        for listener in self._listeners:
            listener(kind, name, data, None)

    @contextmanager
    def event(self, event_id: str) -> Iterator[None]:
        """Everything recorded inside this block belongs to `event_id`."""
        token = _current_event.set(event_id)
        try:
            yield
        finally:
            _current_event.reset(token)

    def record(self, kind: str, name: str, data: dict[str, Any], duration_ms: float | None = None) -> None:
        span = Span(event_id=_current_event.get(), at=utc_now(), kind=kind, name=name, duration_ms=duration_ms,
                    data=data)
        append_jsonl(self._path, span.model_dump(mode="json"))
        for listener in self._listeners:
            listener(kind, name, data, duration_ms)

    def spans(self) -> list[Span]:
        """Every recorded step, oldest first. Parsed once per change of the file (the live UI asks every second)."""
        stat = self._path.stat() if self._path.exists() else None
        version = (stat.st_mtime_ns, stat.st_size) if stat else (0, 0)
        if self._cache is None or self._cache[0] != version:
            self._cache = (version, [Span.model_validate(item) for item in read_jsonl(self._path)])
        return list(self._cache[1])

    def count(self) -> int:
        return len(self.spans())


class NullTracer(Tracer):
    """Records nothing: for code paths built without a trace store."""

    def __init__(self) -> None:
        super().__init__(Path("unused"))

    def record(self, kind: str, name: str, data: dict[str, Any], duration_ms: float | None = None) -> None:
        return None

    def spans(self) -> list[Span]:
        return []

    def count(self) -> int:
        return 0


# --- Wrappers ----------------------------------------------------------------------------


class TracedAgent:
    """Records every run() (and the outreach agent's answer()) of the wrapped agent. Everything else passes through."""

    def __init__(self, agent: Any, tracer: Tracer) -> None:
        self._agent = agent
        self._tracer = tracer

    def __getattr__(self, name: str) -> Any:
        return getattr(self._agent, name)

    def run(self, *args: Any, **kwargs: Any) -> Any:
        return self._traced(self._agent.run, *args, **kwargs)

    def answer(self, *args: Any, **kwargs: Any) -> Any:
        return self._traced(self._agent.answer, *args, **kwargs)

    def _traced(self, method: Any, *args: Any, **kwargs: Any) -> Any:
        start = time.perf_counter()
        inputs = [describe(value) for value in (*args, *kwargs.values())]
        self._tracer.notify("agent_start", self._agent.name, {"inputs": inputs})
        try:
            result = method(*args, **kwargs)
        except Exception as error:
            self._tracer.record("agent", self._agent.name, {"inputs": inputs, "error": f"{type(error).__name__}: {error}"},
                                _ms(start))
            raise
        output = result.output.model_dump(mode="json") if isinstance(result.output, BaseModel) else result.output
        self._tracer.record("agent", self._agent.name, {
            "inputs": inputs, "output": output,
            "proposed": [f"{action.type.value} ({action.id})" for action in result.actions],
        }, _ms(start))
        return result


class TracingLLM:
    """Records the exact prompt, the raw (validated) answer, the model, time and cost of each call."""

    def __init__(self, llm: Any, tracer: Tracer) -> None:
        self._llm = llm
        self._tracer = tracer

    @property
    def label(self) -> str:
        return getattr(self._llm, "model", None) or "scripted answers"

    def complete(self, *, system: str, prompt: str, schema: type[T]) -> T:
        start = time.perf_counter()
        base = {"model": self.label, "schema": schema.__name__, "system": system, "prompt": prompt}
        self._tracer.notify("llm_start", schema.__name__, {"model": self.label})
        try:
            answer = self._llm.complete(system=system, prompt=prompt, schema=schema)
        except Exception as error:
            self._tracer.record("llm", schema.__name__, {**base, "error": f"{type(error).__name__}: {error}"}, _ms(start))
            raise
        self._tracer.record("llm", schema.__name__, {
            **base, "answer": answer.model_dump(mode="json"), "cost_usd": getattr(self._llm, "last_cost_usd", None),
        }, _ms(start))
        return answer


def describe(value: Any) -> str:
    """A short, readable description of an agent input."""
    if isinstance(value, BaseModel) and hasattr(value, "id"):
        extra = getattr(value, "company", None) or getattr(value, "stage", None)
        label = f" ({extra.value if hasattr(extra, 'value') else extra})" if extra else ""
        return f"{type(value).__name__} {value.id}{label}"
    if isinstance(value, BaseModel):
        return type(value).__name__
    if isinstance(value, list):
        return f"{len(value)} {type(value[0]).__name__ if value else 'item'}(s)"
    text = str(value)
    return text if len(text) <= 60 else f"text ({len(text):,} characters)"


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)
