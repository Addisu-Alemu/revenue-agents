"""Deal desk agent: generate an IO (insertion order) from deal data, validate it, propose sending it.

No LLM: a contract must be exact. The IO is filled only from validated deal
fields. If anything required is missing or inconsistent, no IO is drafted
and the problems are returned so a human can fill the gaps.

It proposes `draft_io` (internal, automatic) and `send_io`, which the policy
always routes to a human for approval.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path

from revenue_agents.agents.base import Agent, AgentResult
from revenue_agents.guardrails.policy import ActionType
from revenue_agents.models import Deal, Lead, Model
from revenue_agents.segments import SegmentRegistry

TEMPLATE = Path(__file__).resolve().parents[2] / "data" / "templates" / "io_template.md"
PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


@dataclass(frozen=True)
class Seller:
    name: str = "Daniel Reyes"
    email: str = "daniel.reyes@cool.example"
    signatory: str = "Alex Morgan (VP Sales)"


class IODraft(Model):
    io_number: str
    document: str | None = None
    errors: list[str]


class DealDeskAgent(Agent):
    name = "deal_desk"

    def __init__(self, segments: SegmentRegistry, template: Path = TEMPLATE, seller: Seller = Seller()) -> None:
        self._segments = segments
        self._template = Path(template).read_text()
        self._seller = seller

    def run(self, lead: Lead, deal: Deal, today: dt.date | None = None) -> AgentResult[IODraft]:
        io_number = f"IO-{deal.id}"
        errors = self._validate(lead, deal)
        if errors:
            return AgentResult(output=IODraft(io_number=io_number, errors=errors))

        document = self._render(lead, deal, io_number, today or dt.date.today())
        leftover = sorted(set(PLACEHOLDER.findall(document)))
        if leftover:
            return AgentResult(output=IODraft(io_number=io_number, errors=[f"unfilled template fields: {leftover}"]))

        payload = {"io_number": io_number, "document": document, "to": lead.contact.email}
        actions = [
            self.propose(ActionType.DRAFT_IO, deal, io_number, summary=f"Draft {io_number} for {lead.company}",
                         payload=payload),
            self.propose(ActionType.SEND_IO, deal, io_number,
                         summary=f"Send {io_number} (${deal.fields.budget_usd:,}) to {lead.company}", payload=payload),
        ]
        return AgentResult(output=IODraft(io_number=io_number, document=document, errors=[]), actions=actions)

    def _validate(self, lead: Lead, deal: Deal) -> list[str]:
        fields = deal.fields
        errors = []
        if not fields.budget_usd:
            errors.append("budget is missing")
        if not fields.products:
            errors.append("no products selected")
        unknown = set(fields.products) - set(self._segments.get(lead.segment).products)
        if unknown:
            errors.append(f"products not offered to this segment: {sorted(unknown)}")
        if not (fields.flight_start and fields.flight_end):
            errors.append("flight dates are missing")
        elif fields.flight_end <= fields.flight_start:
            errors.append("flight end must be after flight start")
        if not fields.decision_maker:
            errors.append("signatory (decision maker) is missing")
        if not fields.payment_terms:
            errors.append("payment terms are missing")
        return errors

    def _render(self, lead: Lead, deal: Deal, io_number: str, today: dt.date) -> str:
        fields = deal.fields
        values = {
            "io_number": io_number,
            "issue_date": today.isoformat(),
            "advertiser_name": lead.company,
            "advertiser_contact": f"{lead.contact.name} ({lead.contact.title})",
            "advertiser_email": lead.contact.email,
            "advertiser_signatory": fields.decision_maker,
            "seller_name": self._seller.name,
            "seller_email": self._seller.email,
            "seller_signatory": self._seller.signatory,
            "campaign_name": f"{lead.company}: {fields.scope or 'Campaign'}",
            "flight_start": fields.flight_start.isoformat(),
            "flight_end": fields.flight_end.isoformat(),
            "scope": fields.scope or "As agreed",
            "line_items_table": _line_items(fields.products, fields.budget_usd),
            "total_budget": f"{fields.budget_usd:,}",
            "currency": "USD",
            "payment_terms": fields.payment_terms,
            "contract_term": fields.contract_term or "As per flight dates",
            "special_terms": fields.special_terms or "None",
        }
        return PLACEHOLDER.sub(lambda match: str(values.get(match.group(1), match.group(0))), self._template)


def _line_items(products: list[str], budget: int) -> str:
    """Split the budget evenly across products (remainder on the first line), as a starting point for the rep."""
    share, remainder = divmod(budget, len(products))
    rows = [f"| {product} | {share + (remainder if i == 0 else 0):,} USD |" for i, product in enumerate(products)]
    return "\n".join(["| Product | Amount |", "| --- | --- |", *rows])
