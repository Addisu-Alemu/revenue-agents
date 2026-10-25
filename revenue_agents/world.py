"""The live world: a simulated clock, the things that arrive on time, and the timers the agents set.

The world decides nothing for the agents. It delivers what would arrive in
real life, when it would arrive:
- new leads, at the time they were created in the CRM;
- each prospect's replies, calls and signature, from the buyer simulator, in
  reaction to what the agents actually did;
- the scheduler: an SLA check every morning, a digest every evening;
- the follow-ups the reply agent set, on their dates;
- your decisions (approve, reject, coach, change terms), at once, even while
  the clock is paused.

Each item goes to the orchestrator the way a webhook would. While the world
runs, its clock is the system's clock (storage.utc_now), so stages, approvals,
stall checks and the audit log all agree on the time.

State is one JSON file (world.json): the clock, the speed, the queue and what
was delivered. Any process can read it; one runner at a time delivers
(Runner below, started by the UI, or run_world.py). A delivery that fails
because the model failed pauses the world and stays queued, so pressing play
retries it; nothing is lost.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from revenue_agents.events import Event, digest_event, follow_up_event, lead_event, sla_event
from revenue_agents.llm import ClaudeLLM, LLMError
from revenue_agents.models import Model
from revenue_agents.runtime import ScriptedRuntime
from revenue_agents.simulator import BuyerSimulator
from revenue_agents.storage import (append_jsonl, locked, read_json, read_jsonl, set_clock, try_lock, utc_now,
                                    write_json)
from revenue_agents.system import System, build_system, make_runtime

SPEEDS = {"15 min/s": 900, "1 h/s": 3600, "4 h/s": 14400}  # simulated seconds per real second
DEFAULT_SPEED = 3600
SLA_HOUR, DIGEST_HOUR, FOLLOW_UP_HOUR = 8, 18, 9
RUNNER_LOCK = "world.runner.lock"

Source = Literal["lead", "buyer", "scheduler", "timer", "you"]


class Item(Model):
    """Something that arrives at a time: an event, a buyer's move, or one of your commands."""

    id: str
    at: dt.datetime
    kind: Literal["event", "buyer", "command"]
    source: Source
    label: str
    event: dict[str, Any] | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class Delivered(Model):
    """What arrived, and what the system did with it (the world's log)."""

    id: str
    at: dt.datetime
    label: str
    source: Source
    status: str  # processed, held, duplicate or failed
    steps: list[str] = Field(default_factory=list)
    events: list[str] = Field(default_factory=list)  # the event ids handled: the trace of each is filed under it


class WorldState(Model):
    started: bool = False
    now: dt.datetime
    running: bool = False
    speed: int = DEFAULT_SPEED
    model: Literal["scripted", "claude"] = "scripted"
    queue: list[Item] = Field(default_factory=list)
    done: list[str] = Field(default_factory=list)
    beats: list[str] = Field(default_factory=list)  # buyer beats already scheduled
    working: str | None = None  # the label of what is being delivered right now
    error: str | None = None


class World:
    """The world's state on disk. Every change is a locked read-modify-write, so the UI and the runner can't clash."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        self._path = self.state_dir / "world.json"
        self._log = self.state_dir / "world_log.jsonl"
        self._cache: tuple[int, WorldState] | None = None

    def exists(self) -> bool:
        return self._path.exists()

    def read(self) -> WorldState:
        stamp = self._path.stat().st_mtime_ns if self._path.exists() else 0
        if self._cache is None or self._cache[0] != stamp:
            data = read_json(self._path, None)
            self._cache = (stamp, WorldState.model_validate(data) if data else
                           WorldState(now=dt.datetime.now(dt.timezone.utc)))
        return self._cache[1]

    def now(self) -> dt.datetime:
        """The world's clock. Installed as the system clock while the world runs (storage.set_clock)."""
        return self.read().now

    def log(self) -> list[Delivered]:
        return [Delivered.model_validate(item) for item in read_jsonl(self._log)]

    def upcoming(self, sources: tuple[Source, ...] = ("scheduler", "timer")) -> list[Item]:
        """What is scheduled, soonest first. Buyers' next moves are hidden: the world doesn't announce them."""
        return sorted((item for item in self.read().queue if item.source in sources), key=lambda item: item.at)

    # --- Control ---------------------------------------------------------------------------

    def start(self, leads: list, model: str = "scripted", start: dt.datetime | None = None) -> WorldState:
        """A fresh world: the clock just before the first lead arrives, every lead queued at its creation time."""
        first = min(lead.created_at for lead in leads)
        start = start or first.replace(minute=0, second=0, microsecond=0) - dt.timedelta(minutes=30)
        queue = [Item(id=f"lead:{lead.id}", at=lead.created_at, kind="event", source="lead",
                      label=f"New lead: {lead.company}", event=lead_event(lead).model_dump(mode="json"))
                 for lead in sorted(leads, key=lambda lead: lead.created_at)]
        state = WorldState(started=True, now=start, model=model, queue=queue)
        state.queue += _daily(state)
        with locked(self._path):
            write_json(self._path, state.model_dump(mode="json"))
        self._log.unlink(missing_ok=True)
        return state

    def play(self) -> None:
        self._change(lambda s: s.model_copy(update={"running": True, "error": None}))

    def pause(self) -> None:
        self._change(lambda s: s.model_copy(update={"running": False}))

    def set_speed(self, speed: int) -> None:
        self._change(lambda s: s.model_copy(update={"speed": speed}))

    def set_model(self, model: str) -> None:
        self._change(lambda s: s.model_copy(update={"model": model}))

    def skip(self) -> dt.datetime | None:
        """Jump the clock to the next thing that will arrive."""
        def jump(s: WorldState) -> WorldState:
            later = [item.at for item in s.queue if item.at > s.now]
            return s.model_copy(update={"now": min(later)}) if later else s

        return self._change(jump).now

    def move_to(self, when: dt.datetime) -> None:
        """Move the clock forward to `when` (never back)."""
        self._change(lambda s: s.model_copy(update={"now": when}) if when > s.now else s)

    def submit(self, event: Event, label: str) -> None:
        """Your decision: delivered next, even while the clock is paused."""
        item = Item(id=f"you:{event.id}:{time.time_ns()}", at=self.now(), kind="event", source="you", label=label,
                    event=event.model_dump(mode="json"))
        self.add([item])

    def command(self, name: str, label: str, **data: Any) -> None:
        self.add([Item(id=f"you:{name}:{time.time_ns()}", at=self.now(), kind="command", source="you", label=label,
                       data={"name": name, **data})])

    # --- The runner's side -----------------------------------------------------------------

    def add(self, items: list[Item]) -> None:
        def merge(s: WorldState) -> WorldState:
            known = {item.id for item in s.queue} | set(s.done)
            fresh = [item for item in items if item.id not in known]
            beats = [item.id for item in fresh if item.id.startswith("beat:") and ":forged" not in item.id]
            return s.model_copy(update={"queue": s.queue + fresh, "beats": s.beats + beats})

        if items:
            self._change(merge)

    def take(self) -> Item | None:
        """The next item to deliver: your items first, then whatever is due. The clock moves to its time."""
        chosen: list[Item] = []

        def pick(s: WorldState) -> WorldState:
            if s.working:
                return s
            yours = [item for item in s.queue if item.source == "you"]
            due = sorted((item for item in s.queue if item.source != "you" and item.at <= s.now), key=lambda i: i.at)
            if not (yours or due):
                return s
            chosen.append(yours[0] if yours else due[0])
            return s.model_copy(update={"working": chosen[0].label, "now": max(s.now, chosen[0].at)})

        self._change(pick)
        return chosen[0] if chosen else None

    def finish(self, item: Item, delivered: Delivered) -> None:
        self._change(lambda s: s.model_copy(update={
            "queue": [other for other in s.queue if other.id != item.id], "done": s.done + [item.id], "working": None}))
        append_jsonl(self._log, delivered.model_dump(mode="json"))

    def recover(self) -> None:
        """After a crash mid-delivery the "working" flag stays set and nothing would be delivered again. Only the
        holder of the runner lock calls this, so nothing can be delivering right now: clear it. The item is still
        queued and is delivered again; the orchestrator's claims make that safe."""
        self._change(lambda s: s.model_copy(update={"working": None}) if s.working else s)

    def fail(self, item: Item, error: str) -> None:
        """The model failed: pause, keep the item, and say why. Play retries it."""
        self._change(lambda s: s.model_copy(update={"running": False, "working": None,
                                                    "error": f"{item.label}: {error}"}))

    def advance(self, real_seconds: float) -> None:
        """Move the clock, but never past the next item: things arrive exactly on time."""
        def tick(s: WorldState) -> WorldState:
            if not s.running or s.working:
                return s
            target = s.now + dt.timedelta(seconds=real_seconds * s.speed)
            later = [item.at for item in s.queue if item.at > s.now]
            return s.model_copy(update={"now": min([target, *later])})

        self._change(tick)

    def _change(self, change: Callable[[WorldState], WorldState]) -> WorldState:
        with locked(self._path):
            current = WorldState.model_validate(read_json(self._path, {"now": utc_now().isoformat()}))
            updated = change(current)
            if updated is not current:
                write_json(self._path, updated.model_dump(mode="json"))
        self._cache = None
        return updated


# --- Delivering ------------------------------------------------------------------------------


class Desk:
    """Delivers the world's items to the orchestrator, one at a time, then lets the buyers and the timers react."""

    def __init__(self, state_dir: Path, pace: float = 0.0, voice: ClaudeLLM | None = None) -> None:
        self.state_dir = Path(state_dir)
        self.world = World(self.state_dir)
        self._pace = pace  # scripted mode only: seconds per tool call, so people can watch the agents work
        self._voice = voice
        self._systems: dict[str, System] = {}
        self._simulators: dict[str, BuyerSimulator] = {}
        self.live = LiveBoard(self.state_dir)

    def system(self) -> System:
        model = self.world.read().model
        if model not in self._systems:
            runtime = make_runtime(model)
            if isinstance(runtime, ScriptedRuntime):
                runtime.pace = self._pace
            self._systems[model] = build_system(runtime, state_dir=self.state_dir)
        return self._systems[model]

    def simulator(self) -> BuyerSimulator:
        model = self.world.read().model
        if model not in self._simulators:
            self._simulators[model] = BuyerSimulator(voice=(self._voice or ClaudeLLM()) if model == "claude" else None)
        return self._simulators[model]

    def start(self, model: str = "scripted") -> None:
        set_clock(self.world.now)
        self.world.start(self.system().crm.leads(), model=model)

    def step(self, real_seconds: float = 0.0) -> Delivered | None:
        """Deliver the next due item, or let the clock run if nothing is due."""
        item = self.world.take()
        if item is None:
            self.world.advance(real_seconds)
            return None
        return self.deliver(item)

    def deliver(self, item: Item) -> Delivered:
        system = self.system()
        self.live.begin(item)
        events: list[str] = []
        before = system.tracer.count()
        try:
            with system.tracer.listening(self.live.listener):
                steps, status = self._handle(system, item, events)
            # Commands (release, run now) start work filed under other events: show it under this item.
            events += [span.event_id for span in system.tracer.spans()[before:] if span.event_id not in events]
            events = list(dict.fromkeys(events))
        except Exception as error:
            reason = str(error) if isinstance(error, LLMError) else f"{type(error).__name__}: {error}"
            if item.source != "you":  # pause and keep it: play retries it, so nothing that arrived is lost
                self.world.fail(item, reason)
                self.live.end()
                return Delivered(id=item.id, at=item.at, label=item.label, source=item.source, status="failed",
                                 steps=[f"Paused: {reason}"])
            steps, status = [f"Not done: {reason}"], "failed"  # a decision that can't apply: say so, don't block
        finally:
            self.live.end()
        self.plan(system)
        delivered = Delivered(id=item.id, at=self.world.now(), label=item.label, source=item.source, status=status,
                              steps=steps, events=events)
        self.world.finish(item, delivered)
        return delivered

    def plan(self, system: System) -> None:
        """What happens next: the buyers' reactions, the follow-ups the agents set, and the daily schedule."""
        state = self.world.read()
        self.world.add(self.simulator().react(system, set(state.beats)) + _follow_ups(system) + _daily(state))

    def run_until(self, until: dt.datetime, on_delivered: Callable[[Delivered], None] | None = None,
                  between: Callable[[], None] | None = None) -> list[Delivered]:
        """Deliver everything due up to `until`, as fast as possible (the terminal demo and tests).

        It takes the runner lock for as long as it runs: if the UI or run_world.py already delivers for this folder,
        it refuses rather than deliver the same items twice.
        """
        lock = try_lock(self.state_dir / RUNNER_LOCK)
        if lock is None:
            raise RuntimeError(f"another runner (the UI or run_world.py) already delivers for {self.state_dir}")
        try:
            self.world.recover()
            return self._run_until(until, on_delivered, between)
        finally:
            lock.close()

    def _run_until(self, until: dt.datetime, on_delivered: Callable[[Delivered], None] | None,
                   between: Callable[[], None] | None) -> list[Delivered]:
        delivered = []
        while True:
            if between:
                between()
            item = self.world.take()
            if item is None:
                upcoming = [i.at for i in self.world.read().queue if i.at > self.world.now()]
                if not upcoming or min(upcoming) > until:
                    self.world.move_to(until)
                    return delivered
                self.world.skip()
                continue
            result = self.deliver(item)
            delivered.append(result)
            if on_delivered:
                on_delivered(result)
            if result.status == "failed" and self.world.read().error:
                return delivered

    def _handle(self, system: System, item: Item, handled: list[str]) -> tuple[list[str], str]:
        if item.kind == "command":
            return self._command(system, item.data), "processed"
        events = ([Event.model_validate(item.event)] if item.kind == "event"
                  else self.simulator().deliver(system, item))
        steps, status = [], "processed"
        for event in events:
            handled.append(event.id)
            report = system.orchestrator.handle(event)
            steps += report.steps
            status = report.status
        return steps, status

    def _command(self, system: System, data: dict[str, Any]) -> list[str]:
        if data["name"] == "resume":
            return system.orchestrator.resume(by=data["by"])
        if data["name"] == "run_now":
            return system.orchestrator.run_approved(data["request_id"])
        if data["name"] == "keep_coaching":
            return system.orchestrator.keep_coaching(data["request_id"], by=data["by"])
        if data["name"] == "drop_coaching":
            return system.orchestrator.drop_coaching(data["request_id"], by=data["by"])
        raise ValueError(f"unknown command {data['name']!r}")


def _daily(state: WorldState) -> list[Item]:
    """The scheduler: an SLA check every morning and a digest every evening, today and tomorrow."""
    items = []
    for offset in (0, 1):
        day = (state.now + dt.timedelta(days=offset)).replace(minute=0, second=0, microsecond=0)
        morning, evening = day.replace(hour=SLA_HOUR), day.replace(hour=DIGEST_HOUR)
        items += [Item(id=f"sla:{morning.date()}", at=morning, kind="event", source="scheduler",
                       label="Morning SLA check", event=sla_event(morning).model_dump(mode="json")),
                  Item(id=f"digest:{evening.date()}", at=evening, kind="event", source="scheduler",
                       label="Daily digest", event=digest_event(evening).model_dump(mode="json"))]
    return [item for item in items if item.at >= state.now]


def _follow_ups(system: System) -> list[Item]:
    """Every follow-up an agent scheduled becomes a timer that fires on its date."""
    items = []
    for deal in system.crm.deals():
        lead = system.crm.get_lead(deal.lead_id)
        for activity in system.crm.timeline(deal.id):
            if activity.kind == "follow_up":
                due = dt.date.fromisoformat(activity.data["due"])
                at = dt.datetime(due.year, due.month, due.day, FOLLOW_UP_HOUR, tzinfo=dt.timezone.utc)
                event = follow_up_event(deal.id, activity.data["due"], activity.data["reason"])
                items.append(Item(id=event.id, at=at, kind="event", source="timer",
                                  label=f"Follow up with {lead.contact.name}", event=event.model_dump(mode="json")))
    return items


class LiveBoard:
    """What the desk is doing right now (live.json), for the UI's "Working now" card. Any process can read it."""

    KEEP = 12

    def __init__(self, state_dir: Path) -> None:
        self._path = Path(state_dir) / "live.json"
        self._board: dict[str, Any] = {}

    def read(self) -> dict[str, Any]:
        return read_json(self._path, {})

    def begin(self, item: Item) -> None:
        self._board = {"item": item.label, "source": item.source, "agent": None, "title": None, "steps": []}
        self._save()

    def end(self) -> None:
        self._board = {}
        self._path.unlink(missing_ok=True)

    def listener(self, kind: str, name: str, data: dict[str, Any], duration_ms: float | None) -> None:
        if kind == "agent_start":
            self._board.update(agent=name, title=data.get("title"))
        elif kind == "tool_start":
            self._board["tool"] = name
        elif kind == "tool":
            self._board["steps"] = (self._board.get("steps", []) + [
                {"agent": data.get("agent"), "tool": name, "verdict": data.get("verdict"),
                 "outcome": data.get("outcome"), "result": (data.get("result") or "")[:160]}])[-self.KEEP:]
            self._board["tool"] = None
        elif kind == "agent":
            self._board.update(agent=None, title=None)
        else:
            return
        self._save()

    def _save(self) -> None:
        if self._board:
            write_json(self._path, self._board)


# --- Running in the background -----------------------------------------------------------------


class Runner:
    """Delivers the world in the background: the UI starts one per state folder, run_world.py runs one on its own.

    Only one runner may deliver for a state folder: it holds a lock file for as long as it runs.
    """

    TICK = 0.25  # seconds between clock steps
    GAP = 0.8  # a short pause after each delivery, so people can see one thing at a time

    def __init__(self, desk: Desk) -> None:
        self.desk = desk
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = None

    def start(self) -> bool:
        """Start delivering. False if another runner already delivers for this state folder."""
        self._lock = try_lock(self.desk.state_dir / RUNNER_LOCK)
        if self._lock is None:
            return False
        if self.desk.world.exists():
            self.desk.world.recover()
        set_clock(self.desk.world.now)
        self._thread = threading.Thread(target=self._loop, name="world-runner", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=30)
        if self._lock:
            self._lock.close()

    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        last = time.monotonic()
        while not self._stop.is_set():
            elapsed, last = time.monotonic() - last, time.monotonic()
            if self.desk.world.exists() and self.desk.step(elapsed) is not None:
                self._stop.wait(self.GAP)
                last = time.monotonic()  # the clock stood still while the desk worked
            self._stop.wait(self.TICK)
