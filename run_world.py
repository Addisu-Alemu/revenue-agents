"""Run the live world in the foreground, in real time, printing what arrives and what the agents did.

    python run_world.py                      # scripted agent decisions, 1 simulated hour per second
    python run_world.py --llm claude         # real Claude picks the tools (needs ANTHROPIC_API_KEY in .env)
    python run_world.py --speed 900          # 15 simulated minutes per second
    python run_world.py --fresh              # start a new world (deletes the demo state, keeps the kill switch)

The UI (streamlit run app.py) shows the same world: it shares state/demo, so you can approve things there while
this runs. Only one runner delivers for a state folder; if the UI already runs one, this says so and stops.
Ctrl+C stops it; the world keeps its place and carries on from there next time.
"""

import argparse
import time

from dotenv import load_dotenv

from revenue_agents.demo import DEMO_STATE
from revenue_agents.system import reset_state
from revenue_agents.world import Desk, Runner


def main() -> int:
    parser = argparse.ArgumentParser(description="Revenue Agents live world")
    parser.add_argument("--llm", choices=["scripted", "claude"], default="scripted", help="model to use")
    parser.add_argument("--speed", type=int, default=3600, help="simulated seconds per real second (default 3600)")
    parser.add_argument("--fresh", action="store_true", help="start a new world")
    args = parser.parse_args()

    load_dotenv()
    if args.fresh:
        reset_state(DEMO_STATE)
    desk = Desk(DEMO_STATE, pace=0.3)
    if not desk.world.exists():
        desk.start(model=args.llm)
    desk.world.set_model(args.llm)
    desk.world.set_speed(args.speed)
    runner = Runner(desk)
    if not runner.start():
        print("Another runner (probably the UI) is already delivering for state/demo.")
        return 1
    desk.world.play()
    print(f"World running from {desk.world.now():%a %d %b %H:%M} UTC. Ctrl+C to stop.")
    shown = len(desk.world.log())
    try:
        while True:
            log = desk.world.log()
            for delivered in log[shown:]:
                print(f"\n{delivered.at:%a %d %b %H:%M} · {delivered.label} ({delivered.status})")
                for step in delivered.steps:
                    print(f"  - {step}")
            shown = len(log)
            error = desk.world.read().error
            if error:
                print(f"\nPaused: {error}")
                return 1
            time.sleep(0.5)
    except KeyboardInterrupt:
        desk.world.pause()
        print("\nStopped. The world keeps its place.")
        return 0
    finally:
        runner.stop()


if __name__ == "__main__":
    raise SystemExit(main())
