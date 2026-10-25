"""The Playbook's content: every lesson with the words it came from and where it was used, how emails with a lesson
did against emails without one, and each segment pack's key settings.

Plain functions with no Streamlit, read only from what the system recorded.
"""

from __future__ import annotations

from dataclasses import dataclass

from revenue_agents.models import Lesson
from revenue_agents.system import System

ENOUGH_TO_JUDGE = 20  # first emails on each side before a reply rate means anything


@dataclass
class LessonCard:
    lesson: Lesson
    origin: str  # "From the intro call, Maya Thompson at 00:01:28"
    used_in: list[str]


@dataclass
class Effect:
    with_lesson: tuple[int, int]  # (first emails sent, replied)
    without: tuple[int, int]

    @property
    def enough(self) -> bool:
        return min(self.with_lesson[0], self.without[0]) >= ENOUGH_TO_JUDGE


@dataclass
class PackCard:
    name: str
    title: str
    file: str
    lines: list[str]


def lessons(system: System, segment: str = "all") -> list[LessonCard]:
    """Newest first, each with where it came from and every place it shaped."""
    from revenue_agents.ui.components import used_in  # the same wording as everywhere else in the UI

    cards = []
    for lesson in sorted(system.lessons.all(), key=lambda item: item.created_at, reverse=True):
        if segment != "all" and not lesson.applies_in(segment):
            continue
        places = list(dict.fromkeys(used_in(system, use) for use in system.lessons.uses(lesson.id)))
        cards.append(LessonCard(lesson, origin(system, lesson), places))
    return cards


def origin(system: System, lesson: Lesson) -> str:
    who = f", {lesson.said_by}" if lesson.said_by else ""
    when = f" at {lesson.said_at}" if lesson.said_at else ""
    if lesson.source == "call":
        call = ("the intro call" if "intro" in lesson.source_id else
                "the discovery call" if "discovery" in lesson.source_id else f"call {lesson.source_id}")
        return f"From {call}{who}{when}"
    if lesson.source == "reply":
        return f"From a reply ({lesson.source_id}){who}"
    if lesson.source == "review":
        try:
            summary = system.approvals.get(lesson.source_id).action.summary
        except KeyError:
            summary = lesson.source_id
        return f"From a reviewer's note{who}, on “{summary}”"
    return f"From {lesson.source} {lesson.source_id}"


def effect(system: System) -> Effect:
    """First emails that applied a lesson against those that didn't, and how many got any reply after them."""
    counts = {True: [0, 0], False: [0, 0]}
    for deal in system.crm.deals():
        messages = sorted(system.crm.messages(deal.id), key=lambda message: message.at)
        first = next((m for m in messages if m.direction == "outbound" and not m.subject.lower().startswith("re:")),
                     None)
        if first is None:
            continue
        side = counts[bool(first.meta.get("lessons_used"))]
        side[0] += 1
        side[1] += any(m.direction == "inbound" and m.at > first.at for m in messages)
    return Effect(with_lesson=tuple(counts[True]), without=tuple(counts[False]))


def packs(system: System) -> list[PackCard]:
    cards = []
    for name in system.segments.names():
        pack = system.segments.get(name)
        weights, tiers = pack.scoring.weights, pack.scoring.tiers
        asks = "/".join(tier.value for tier in pack.autonomy.first_touch_approval_tiers)
        cards.append(PackCard(name, pack.display_name, f"{name}.yaml", [
            f"Weights: fit {weights.fit:g}, intent {weights.intent:g}, engagement {weights.engagement:g}",
            f"Tier A from {tiers.A:g}, Tier B from {tiers.B:g}",
            f"First contact needs you for Tier {asks}",
            f"Tone: {pack.messaging.tone}",
        ]))
    return cards
