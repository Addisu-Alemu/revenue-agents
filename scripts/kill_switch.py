"""Pause or resume every agent action. Works on Windows, macOS and Linux.

    python scripts/kill_switch.py on --reason "Bad emails going out"
    python scripts/kill_switch.py off          # replays held events, runs approved actions that waited
    python scripts/kill_switch.py status

Uses the demo state (state/demo) by default; pass --state-dir for another one.
"""

import argparse
import getpass
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from revenue_agents.demo import DEMO_STATE  # noqa: E402
from revenue_agents.system import build_system, make_runtime  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Revenue Agents kill switch")
    parser.add_argument("command", choices=["on", "off", "status"])
    parser.add_argument("--reason", default="no reason given")
    parser.add_argument("--by", default=getpass.getuser(), help="who is flipping the switch (audited)")
    parser.add_argument("--state-dir", default=str(DEMO_STATE))
    parser.add_argument("--llm", choices=["scripted", "claude"], default="scripted", help="model used to replay held events")
    args = parser.parse_args(argv)

    load_dotenv()
    system = build_system(make_runtime(args.llm), state_dir=Path(args.state_dir))
    orchestrator = system.orchestrator
    if args.command == "on":
        lines = orchestrator.pause(args.reason, by=args.by)
    elif args.command == "off":
        lines = orchestrator.resume(by=args.by)
    else:
        engaged = system.kill_switch.is_engaged()
        lines = [f"Kill switch: {'ON (' + system.kill_switch.reason() + ')' if engaged else 'OFF'}",
                 f"Held events: {len(orchestrator.held_events())}",
                 f"Approved, not executed: {len(orchestrator.approved_waiting())}"]
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
