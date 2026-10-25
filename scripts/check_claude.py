"""Smoke test: prove the API key works, and that Claude really drives the agents' tools.

Run from the project folder:
    python scripts/check_claude.py            # one tiny question; costs a fraction of a cent
    python scripts/check_claude.py --agent    # one real agent run each: research, then outreach (capped at $1)

--agent runs in a throwaway state folder. Claude picks the tools; the hook asks the policy for each call. It prints
every tool call with the hook's verdict and rule, then checks what must hold: the research agent saved a score, and
the outreach agent's email to a Tier A lead stopped for a human (deferred) instead of going out.
"""

import argparse
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from revenue_agents.llm import DEFAULT_MODEL, ClaudeLLM, LLMError  # noqa: E402
from revenue_agents.runtime import ClaudeRuntime, RunStatus, Verdict  # noqa: E402
from revenue_agents.system import build_system  # noqa: E402


class Ping(BaseModel):
    capital: str


def ping() -> int:
    llm = ClaudeLLM(DEFAULT_MODEL, max_budget_usd=0.10)
    print(f"Asking {llm.model}...")
    try:
        answer = llm.complete(system="Answer with JSON only.", prompt="What is the capital of France?", schema=Ping)
    except LLMError as error:
        print(f"FAILED: {error}")
        return 1
    print(f"OK: Claude says the capital of France is {answer.capital}.")
    if llm.last_cost_usd is not None:
        print(f"Cost of this call: ${llm.last_cost_usd:.4f}")
    return 0


def agent_runs() -> int:
    with tempfile.TemporaryDirectory() as folder:
        system = build_system(ClaudeRuntime(DEFAULT_MODEL, max_budget_usd=0.50), state_dir=Path(folder))
        agents = system.orchestrator._agents
        lead = system.crm.get_lead("L-001")
        print(f"Research agent on {lead.company}, with {DEFAULT_MODEL}...")
        try:
            research = agents.host.run(agents.research.task(lead, system.crm.deal_for_lead(lead.id)))
            _show(research)
            deal = system.crm.get_deal("D-001")
            if deal.score is None:
                print("FAILED: the research agent didn't save a score.")
                return 1
            print(f"\nOutreach agent on {lead.company} (Tier {deal.score.tier.value})...")
            outreach = agents.host.run(agents.outreach.first_touch(lead, deal))
            _show(outreach)
        except LLMError as error:
            print(f"FAILED: {error}")
            return 1

        sends = outreach.called("send_email")
        deferred = any(call.verdict is Verdict.DEFER for call in sends)
        sent = [a for a in system.crm.timeline("D-001") if a.kind == "email_sent"]
        if deal.score.tier.value == "A" and (not deferred or sent or outreach.status is not RunStatus.WAITING):
            print("FAILED: the Tier A email should have stopped for a human, and nothing should have been sent.")
            return 1
        cost = (research.cost_usd or 0) + (outreach.cost_usd or 0)
        print(f"\nOK: Claude chose the tools; the policy decided each call; the Tier A email waits for a human."
              f"\nCost of both runs: ${cost:.4f}")
        return 0


def _show(run) -> None:
    for call in run.calls:
        outcome = f", {call.outcome.value}" if call.outcome else ""
        print(f"  {call.tool:<20} {call.verdict.value:<6} [{call.rule}{outcome}] {call.result[:90]!r}")
    print(f"  -> {run.status.value}; the agent said: {run.summary[:200]!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Check the Claude connection")
    parser.add_argument("--agent", action="store_true", help="also run the research and outreach agents once, for real")
    args = parser.parse_args()
    load_dotenv()
    code = ping()
    return code if code or not args.agent else agent_runs()


if __name__ == "__main__":
    raise SystemExit(main())
