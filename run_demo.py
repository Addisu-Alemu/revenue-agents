"""Run the live demo in the terminal: the world runs at full speed, and you decide what waits for a human.

    python run_demo.py                         # scripted agent decisions, both segments
    python run_demo.py --llm claude            # real Claude picks the tools (needs ANTHROPIC_API_KEY in .env)
    python run_demo.py --segment agency        # show one segment's deals (each segment is one YAML pack)
    python run_demo.py --auto-approve          # don't pause for approvals
    python run_demo.py --days 7                # how much simulated time to run (default 14)
"""

import argparse

from dotenv import load_dotenv

from revenue_agents.demo import DEMO_STATE, run


def main() -> int:
    parser = argparse.ArgumentParser(description="Revenue Agents live demo")
    parser.add_argument("--llm", choices=["scripted", "claude"], default="scripted", help="model to use (default: scripted)")
    parser.add_argument("--segment", default=None, help="only show this segment's deals (default: all)")
    parser.add_argument("--auto-approve", action="store_true", help="approve high-risk actions without asking")
    parser.add_argument("--keep-state", action="store_true", help="don't reset the demo state before running")
    parser.add_argument("--days", type=int, default=14, help="simulated days to run (default: 14)")
    args = parser.parse_args()

    load_dotenv()
    return run(segment=args.segment, llm_mode=args.llm, state_dir=DEMO_STATE, auto_approve=args.auto_approve,
               fresh=not args.keep_state, days=args.days)


if __name__ == "__main__":
    raise SystemExit(main())
