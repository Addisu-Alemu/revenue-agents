"""Copilot: takes a goal from you in plain words and works across deals with the same tools and rules.

It can read everything (deals, threads, timers, what waits for you, lessons) and change only two low-risk
things: set a follow-up timer, or flag a deal for the rep with a note. Both go through the PreToolUse hook and
the gateway like every other agent's tools. Anything else (an email, a contract) it leaves to you or to the
agent that owns it. Rules in code refuse what it shouldn't do: no second follow-up where one is already set,
no follow-up on a closed deal, no date in the past.
"""

from __future__ import annotations

import datetime as dt
import re

from pydantic import Field

from revenue_agents.agents.base import Agent
from revenue_agents.crm import CRM
from revenue_agents.guardrails.approvals import ApprovalQueue
from revenue_agents.guardrails.policy import ActionType
from revenue_agents.guardrails.untrusted import UNTRUSTED_RULES, wrap_untrusted
from revenue_agents.lessons import LessonStore
from revenue_agents.models import Deal, Model, Stage
from revenue_agents.runtime import AgentTask, Script, Tool, ToolResult
from revenue_agents.storage import utc_now

CLOSED = (Stage.HANDOFF, Stage.CLOSED_LOST)
NOT_CONTACTED = (Stage.PROSPECT, Stage.ENRICHED, Stage.SCORED)  # we never emailed them: nothing to follow up
FOLLOW_UP_WINDOW_DAYS = 180

SYSTEM_PROMPT = f"""You are the copilot for a sales team at Cool AI, an AI advertising platform. A person gives you a
goal in plain words; you work across the deals to reach it.

{UNTRUSTED_RULES}

Work with your tools: search the deals, read the ones that matter, look at what the team has learned, then act.
You may change only two things: set_follow_up (a dated reminder for the rep) and flag_for_rep (a note on the deal).
Never set a follow-up where one is already set or on a lead we never contacted, and leave a deal alone if something
already waits for the person:
say so instead. Anything else (writing an email, changing a contract) is for the person or the agent that owns it:
say what you'd suggest. When you are done, reply with a few short sentences saying what you did for each deal and
why, and stop."""


class SearchDeals(Model):
    flag: str | None = Field(default=None, description="Only deals with this flag, e.g. pricing_requested")
    stage: str | None = Field(default=None, description="Only deals at this stage, e.g. outreach")
    quiet_for_days: int | None = Field(default=None, ge=0, description="Only deals with no activity for this long")
    segment: str | None = None


class DealRef(Model):
    deal_id: str


class LessonQuery(Model):
    text: str = Field(default="", description="Words to look for; empty returns every lesson")


class FlagForRep(Model):
    deal_id: str
    reason: str = Field(max_length=300)


class SetFollowUp(Model):
    deal_id: str
    on: dt.date = Field(description="The day to follow up, YYYY-MM-DD")
    reason: str = Field(max_length=300)


class CopilotPlan(Model):
    """The scripted model's reading of a goal: which deals to look at, and which lessons might help."""

    search: SearchDeals = Field(default_factory=SearchDeals)
    lessons: str = ""
    follow_up_in_days: int = 2


class CopilotAgent(Agent):
    name = "copilot"

    def __init__(self, crm: CRM, lessons: LessonStore, approvals: ApprovalQueue) -> None:
        self._crm = crm
        self._lessons = lessons
        self._approvals = approvals

    def task(self, goal: str, ask_id: str) -> AgentTask:
        key = ask_id.replace(":", "-")

        def search(args: SearchDeals) -> ToolResult:
            found = [self._about(deal) for deal in self._crm.deals() if self._matches(deal, args)]
            lines = [f"- {d['id']} {d['company']} ({d['segment']}): {d['stage']}, quiet {d['quiet_days']} days, "
                     f"flags: {', '.join(d['flags']) or 'none'}"
                     + (f", waits for the person: {'; '.join(d['waiting'])}" if d["waiting"] else "")
                     + (f", follow-up set for {d['follow_up']}" if d["follow_up"] else "") for d in found]
            return ToolResult(text=f"{len(found)} deal(s):\n" + ("\n".join(lines) or "none"), data={"deals": found})

        def read(args: DealRef) -> ToolResult:
            deal = self._deal(args.deal_id)
            if deal is None:
                return ToolResult(text=f"No deal {args.deal_id}", is_error=True)
            about = self._about(deal)
            thread = []
            for message in self._crm.messages(deal.id)[-4:]:
                body = " ".join(message.body.split())[:400]
                who = "We wrote" if message.direction == "outbound" else "They wrote"
                text = body if message.direction == "outbound" else wrap_untrusted(body, f"message:{message.id}")
                thread.append(f"[{message.at:%Y-%m-%d}] {who}: {text}")
            known = {name: value for name, value in deal.fields.model_dump(mode="json").items() if value}
            return ToolResult(text=(f"{deal.id} {about['company']}: stage {deal.stage.value}, quiet "
                                    f"{about['quiet_days']} days, flags {', '.join(deal.flags) or 'none'}\n"
                                    f"Waits for the person: {'; '.join(about['waiting']) or 'nothing'}\n"
                                    f"Follow-up set: {about['follow_up'] or 'none'}\nFields: {known or 'none'}\n"
                                    f"Last messages:\n" + ("\n".join(thread) or "none")), data=about)

        def lessons(args: LessonQuery) -> ToolResult:
            words = set(re.findall(r"[a-z]+", args.text.lower())) - {"a", "the", "and", "of", "to"}
            found = [lesson for lesson in self._lessons.all()
                     if not words or words & set(re.findall(r"[a-z]+", lesson.text.lower()))]
            lines = [f"- [{lesson.id}] {lesson.text} ({lesson.scope})" for lesson in found]
            return ToolResult(text="\n".join(lines) or "No lesson matches.", data={"lessons": [l.id for l in found]})

        def follow_up_rule(args: SetFollowUp) -> str | None:
            deal = self._deal(args.deal_id)
            if deal is None:
                return f"No deal {args.deal_id}"
            if deal.stage in CLOSED:
                return f"{deal.id} is closed: nothing to follow up"
            if deal.stage in NOT_CONTACTED:
                return f"{deal.id} was never contacted: nothing to follow up"
            if self._follow_up(deal):
                return f"{deal.id} already has a follow-up set for {self._follow_up(deal)}"
            today = utc_now().date()
            if not today < args.on <= today + dt.timedelta(days=FOLLOW_UP_WINDOW_DAYS):
                return f"Pick a date in the next {FOLLOW_UP_WINDOW_DAYS} days (today is {today})"
            return None

        def follow_up(args: SetFollowUp):
            deal = self._deal(args.deal_id)
            company = self._crm.get_lead(deal.lead_id).company
            return self.propose(ActionType.SCHEDULE_FOLLOW_UP, deal, key=key,
                                summary=f"Follow up with {company} on {args.on:%a %d %b %Y}: {args.reason}",
                                payload={"due": args.on.isoformat(), "reason": args.reason, "asked_in": ask_id})

        def flag_rule(args: FlagForRep) -> str | None:
            return None if self._deal(args.deal_id) else f"No deal {args.deal_id}"

        def flag(args: FlagForRep):
            deal = self._deal(args.deal_id)
            return self.propose(ActionType.FLAG_DEAL, deal, key=key, summary=f"Copilot note for the rep: {args.reason}",
                                payload={"flags": ["copilot_note"], "reason": args.reason, "asked_in": ask_id})

        def script(s: Script) -> None:
            plan = s.ask(CopilotPlan)
            found = s.call("search_deals", **plan.search.model_dump(exclude_none=True)).data.get("deals", [])
            if not found:
                s.say("No deal matches that goal, so I changed nothing.")
                return
            for deal in found:
                s.call("read_deal", deal_id=deal["id"])
            s.call("search_lessons", text=plan.lessons)
            done = []
            for deal in found:
                if deal["follow_up"]:
                    done.append(f"{deal['company']}: skipped, a follow-up is already set for {deal['follow_up']}.")
                elif deal["waiting"]:
                    done.append(f"{deal['company']}: left alone, something already waits for you.")
                elif deal["stage"] in (stage.value for stage in CLOSED):
                    done.append(f"{deal['company']}: closed, nothing to do.")
                elif deal["stage"] in (stage.value for stage in NOT_CONTACTED):
                    done.append(f"{deal['company']}: never contacted (Tier C goes to nurture), so no follow-up.")
                else:
                    on = utc_now().date() + dt.timedelta(days=plan.follow_up_in_days)
                    s.call("set_follow_up", deal_id=deal["id"], on=on.isoformat(), reason=f"From your goal: {goal}")
                    s.call("flag_for_rep", deal_id=deal["id"], reason=f"Quiet for {deal['quiet_days']} days; "
                                                                      f"follow-up set for {on:%a %d %b}.")
                    done.append(f"{deal['company']}: follow-up set for {on:%a %d %b} and flagged for the rep.")
            s.say(" ".join(done))

        tools = [
            Tool("search_deals", "Find deals by flag, stage, segment, or how long they have been quiet.", SearchDeals,
                 run=search),
            Tool("read_deal", "Read one deal: stage, flags, what waits for the person, timers, fields and the last "
                              "messages.", DealRef, run=read),
            Tool("search_lessons", "Look for what the team has learned that fits.", LessonQuery, run=lessons),
            Tool("set_follow_up", "Set a dated reminder for the rep on a deal.", SetFollowUp, action=follow_up,
                 check=follow_up_rule, proposes=ActionType.SCHEDULE_FOLLOW_UP),
            Tool("flag_for_rep", "Leave a note for the rep on a deal.", FlagForRep, action=flag, check=flag_rule,
                 proposes=ActionType.FLAG_DEAL),
        ]
        return AgentTask(agent=self.name, title=f"Goal: {goal}", deal_id="", system=SYSTEM_PROMPT,
                         prompt=f"Goal from the person: {goal}\nToday is {utc_now():%Y-%m-%d}. Reach the goal.",
                         tools=tools, script=script, max_turns=20)

    # --- Helpers -------------------------------------------------------------------------

    def _deal(self, deal_id: str) -> Deal | None:
        try:
            return self._crm.get_deal(deal_id)
        except KeyError:
            return None

    def _matches(self, deal: Deal, args: SearchDeals) -> bool:
        if args.flag and args.flag not in deal.flags:
            return False
        if args.stage and deal.stage.value != args.stage:
            return False
        if args.segment and deal.segment != args.segment:
            return False
        return args.quiet_for_days is None or self._quiet_days(deal) >= args.quiet_for_days

    def _about(self, deal: Deal) -> dict:
        return {"id": deal.id, "company": self._crm.get_lead(deal.lead_id).company, "segment": deal.segment,
                "stage": deal.stage.value, "flags": deal.flags, "quiet_days": self._quiet_days(deal),
                "waiting": [r.action.summary for r in self._approvals.pending() if r.action.deal_id == deal.id],
                "follow_up": self._follow_up(deal)}

    def _quiet_days(self, deal: Deal) -> int:
        """Days since the last email either way (reminders and flags aren't conversation), else since it arrived."""
        last = max((message.at for message in self._crm.messages(deal.id)), default=None)
        if last is None:
            last = self._crm.get_lead(deal.lead_id).created_at
        return max(int((utc_now() - last).total_seconds() // 86400), 0)

    def _follow_up(self, deal: Deal) -> str | None:
        today = utc_now().date()
        due = [a.data.get("due") for a in self._crm.timeline(deal.id) if a.kind == "follow_up"]
        future = sorted(day for day in due if day and dt.date.fromisoformat(day) > today)
        return future[0] if future else None
