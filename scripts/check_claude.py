"""Smoke test: prove the API key works by asking Claude one tiny question.

Run from the project folder:  python scripts/check_claude.py
Costs a fraction of a cent.
"""

import sys
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from revenue_agents.llm import DEFAULT_MODEL, ClaudeLLM, LLMError  # noqa: E402


class Ping(BaseModel):
    capital: str


def main() -> int:
    load_dotenv()
    llm = ClaudeLLM(DEFAULT_MODEL, max_budget_usd=0.10)
    print(f"Asking {llm.model}...")
    try:
        answer = llm.complete(
            system="Answer with JSON only.",
            prompt="What is the capital of France?",
            schema=Ping,
        )
    except LLMError as error:
        print(f"FAILED: {error}")
        return 1

    print(f"OK: Claude says the capital of France is {answer.capital}.")
    if llm.last_cost_usd is not None:
        print(f"Cost of this call: ${llm.last_cost_usd:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
