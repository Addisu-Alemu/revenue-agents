"""Run the live demo.

    python run_demo.py                         # fake model (scripted answers), brand segment
    python run_demo.py --llm claude            # real Claude (needs ANTHROPIC_API_KEY with credit in .env)
    python run_demo.py --segment agency        # another segment: needs only segments/agency.yaml
    python run_demo.py --auto-approve          # don't pause for approvals
"""

import argparse

from dotenv import load_dotenv

from revenue_agents.demo import DEMO_STATE, run


def main() -> int:
    parser = argparse.ArgumentParser(description="Revenue Agents live demo")
    parser.add_argument("--llm", choices=["fake", "claude"], default="fake", help="model to use (default: fake)")
    parser.add_argument("--segment", default="brand", help="segment pack to run (default: brand)")
    parser.add_argument("--auto-approve", action="store_true", help="approve high-risk actions without asking")
    parser.add_argument("--keep-state", action="store_true", help="don't reset the demo state before running")
    args = parser.parse_args()

    load_dotenv()
    return run(segment=args.segment, llm_mode=args.llm, state_dir=DEMO_STATE, auto_approve=args.auto_approve,
               fresh=not args.keep_state)


if __name__ == "__main__":
    raise SystemExit(main())
