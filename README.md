# Revenue Agents

An agentic revenue system for Cool AI. **The agents run the desk; you run the agents.** AI agents work a live, simulated world: leads arrive on their own, prospects reply to what the agents actually wrote, calls and signatures happen when they would. Each agent works in a loop with its own small set of tools and decides which to call; every call passes a hook that asks the autonomy policy, written in code, whether it runs on its own, waits for you, or is refused. You only see what the rules send you, and you can coach, approve, reject or pause everything. Built on the Claude Agent SDK, with HubSpot, the inbox, the calendar and e-signature mocked.

Design document: https://claude.ai/artifact/NN3rv38qkcyhZjwnuUUYDf · the October redesign and its decisions: [docs/DESIGN.md](docs/DESIGN.md) · reviewing this change: [docs/REVIEW_GUIDE.md](docs/REVIEW_GUIDE.md)

> **Status.** Four AI agents with tools (research, outreach, reply, call intel), three code skills on purpose (scoring, call brief, deal desk: tiers and contracts must be exact), the autonomy policy as a PreToolUse hook in front of the action gateway, the approval inbox, audit log and kill switch, stage tracking, the daily digest and e-signature through to handoff. A live world with a clock, a scheduler and a buyer simulator. Two segments, agencies added through `segments/agency.yaml` alone. Runs in the browser, in the terminal, or headless, with real Claude or with scripted decisions.

## How it works

```
the world (world.py)            leads arrive · buyers react (simulator.py) · SLA check 08:00, digest 18:00 · follow-up timers
      |                          your decisions go in the same queue, and are delivered first
      v
orchestrator                    owns deal state and stages; gives each event to the right agent; contracts, signatures,
      |                          unsubscribes and IO versions stay in code
      v
agent (runtime.py)              the model picks a tool -> PreToolUse hook -> policy (guardrails/policy.py)
      |                              allow: the tool runs; a side effect goes through the gateway (claims, audit)
      |                              defer: the run stops; the call waits in your inbox
      |                              deny:  the reason goes back to the model, which can try something else
      v
gateway -> handlers -> mock HubSpot / outbox / calendar / e-sign
```

- **Real Claude** (`claude`): each agent is a Claude Agent SDK run. Its tools are an in-process MCP server; no built-in tools (no files, shell or web), no personal settings, only that agent's tools allowed (`dontAsk`), a turn limit and a spend cap per run.
- **Scripted decisions** (`scripted`, the default): the same agents, tools, hook, policy and gateway; only the decisions (what to write, how to read a reply, when to meet) come from pre-written answers for the mock data. No API key needed. The UI always says which mode is on.

## Quick start with Docker (any OS: Windows, macOS, Linux)

Needs only [Docker](https://docs.docker.com/get-docker/). No Python setup.

```bash
docker compose up --build            # UI at http://localhost:8501
```

```bash
docker compose run --rm app python run_demo.py --auto-approve     # terminal demo
docker compose run --rm app pytest                                # tests
docker compose exec app python scripts/kill_switch.py on --reason "Investigating"   # while the UI runs
```

- **API key (optional, only for `--llm claude` / the "claude" model in the UI):** put `ANTHROPIC_API_KEY=...` in a `.env` file next to `docker-compose.yml`. Compose passes it in at run time; it is never copied into the image (`.dockerignore`).
- **State** (deals, approvals, lessons, audit log) lives in a Docker volume, so it survives restarts. `docker compose down -v` deletes it.
- The container runs as a normal user, not root, and has a health check.

## Setup (without Docker)

Requires **Python 3.10+** (developed on 3.11). Works on Windows, macOS and Linux. An **Anthropic API key** is only needed for the real model (`--llm claude`); the default scripted mode needs none.

```bash
git clone https://github.com/Addisu-Alemu/revenue-agents.git
cd revenue-agents

# Create and activate a virtual environment (uv shown; `python -m venv .venv` works too)
uv venv -p 3.11 .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# Install dependencies
uv pip install -r requirements.txt
```

### API key

Create a file named `.env` in the project folder containing one line:

```
ANTHROPIC_API_KEY=sk-ant-...
```

- `.env` is listed in `.gitignore`, so it is never committed. Check with `git check-ignore .env`.
- Create it in your editor rather than with `echo` in the terminal, so the key doesn't end up in your shell history.
- Only the entry-point scripts load `.env`. Tests never do, so the test suite can't call the real API.
- The code refuses to call Claude without `ANTHROPIC_API_KEY`. It never falls back to whatever Claude Code account is logged in on the machine.

Check the connection (one tiny request, costs a fraction of a cent), and that Claude really drives the tools (one research and one outreach run, capped at $1; it prints every tool call with the hook's verdict, and checks that the Tier A email stopped for a human):

```bash
python scripts/check_claude.py
python scripts/check_claude.py --agent
```

## Run it

### In the browser (the live world)

```bash
streamlit run app.py
```

Open http://localhost:8501 and press **Start**. The clock starts on Thursday 1 October; play, pause, speed it up (15 min, 1 h or 4 h of world time per second), or jump to the next arrival.

- **Today**: what needs you, who is working right now (each tool call appears as the agent makes it), what happened (every arrival with the agent runs it started and their tool calls), what's coming up (the scheduler and the timers agents set), and the pipeline. **Pause all agents** is the kill switch: events keep arriving and wait in order; nothing reaches a customer or the CRM until you release them.
- **Inbox**: only what the rules sent you, why it stopped (the rule), the draft (edit it before approving if you like), **Redraft** with your note (and keep it as a lesson for every later email), and what the agent did and used to get there.
- **Team**: the agents, each tool and its permission, the rules table and the audit log. All of it is generated from the code.
- **Pipeline**, **Deal detail** (the conversation, research note, meeting, call brief, the call, IO versions) and **Agent trace** (any event, step by step: each agent's tool calls in order, the rule behind each, what the gateway executed, how the deal moved).

One background runner delivers the world for the state folder (`state/demo`, shared with the terminal tools). Your decisions go through the same queue, so you and the agents never act on the same deal at once.

### In the terminal

```bash
python run_demo.py                  # the world at full speed; asks you at each approval
python run_demo.py --auto-approve   # approve everything
python run_demo.py --segment agency # show only one segment's deals
python run_demo.py --llm claude     # real Claude picks the tools (needs ANTHROPIC_API_KEY with credit in .env)
python run_world.py                 # the world in real time (1 h per second), printing what arrives; approve in the UI
```

What to watch for, in order: BrightBite's Tier A first email waits for you; Maya asks a question first, and the reply agent drafts an answer that waits too (an answer to a top-tier lead); her next reply asks for a call, so the reply agent **books the meeting** on its own and the deal moves to Meeting with a call brief (her email provider delivers that webhook twice: handled once); the intro call leaves budget and dates open, so the brief is updated for the second call; the call with the CFO fills them in, lessons are learned, and the IO waits for you; Maya asks for Net 45, so the deal desk drafts **IO v2** for your approval (approving voids v1, so only the new one can be signed); a forged "signed" webhook is rejected and Ken's real signature closes the deal. Meanwhile Owen's reply carries a prompt injection (flagged, nothing learned, a **follow-up timer** set for after the holidays), Sam asks for a rate card (a human answers pricing), Lumen goes quiet and the SLA check flags it, and the two leads that arrive after the calls get emails that **apply what the calls taught**, one of them in the other segment (a global lesson).

Scripted mode is announced as scripted. `--llm claude` never falls back to scripted answers: if the key is missing or has no credit, the run stops with an error, and in the UI the world pauses with the reason and retries when you press Play.

## Tests

```bash
pytest                                                  # all tests, offline and free
pytest --cov=revenue_agents --cov-report=term-missing   # with coverage
```

Tests run the agents with the scripted runtime: the real tools, hook, policy, gateway and handlers, with pre-written decisions (`FakeLLM`). The Claude runtime is tested against a stand-in for the SDK's `query()` that calls the hook and the tools the way Claude Code does. No API key or network, and the same result every run. The world tests move the clock themselves (no background thread), and the UI tests use Streamlit's AppTest with the runner off.

## Project structure

```
revenue_agents/
  runtime.py         Agents as tool loops: Tool, the PreToolUse guard, ClaudeRuntime (Agent SDK), ScriptedRuntime
  agents/            research, outreach, reply, call_intel (AI, with tools); call_brief, deal_desk (code skills)
  orchestrator.py    Owns deal state; gives each event to the right agent; moves stages
  world.py           The live world: clock, queue, scheduler, timers, the desk that delivers, the background runner
  simulator.py       Buyer simulator (demo only): prospects react to what the agents did
  team.py            The agent team and its rules as data, read from the code (for the Team page)
  models.py          Shared data models: Lead, Deal, LeadScore, Stage, Tier, ...
  segments.py        Loads and validates segment config packs
  llm.py             LLM interface: ClaudeLLM (structured answers, used by the buyer simulator) and FakeLLM
  crm.py             CRM interface + MockHubSpot (system of record: leads, deals, timeline, conversation)
  lessons.py         Lesson store: knowledge loop memory, with usage tracking
  handlers.py        What executed actions actually do (the only side-effecting code)
  stages.py          Stage order and exit criteria
  events.py          Event types (triggers) and their builders
  system.py          Composition root: build_system() and make_runtime()
  tracking.py        Stall detection, reminders, daily digest
  esign.py           E-signature interface + mock provider
  demo.py            The terminal demo (the world at full speed); demo_llm.py holds the scripted decisions
  storage.py         Atomic, locked file storage for runtime state; the world clock
  tracing.py         The agent trace: every run, tool call, policy decision, execution and stage change
  ui/                Browser UI: today, inbox, pipeline, deal detail, team, agent trace
  skills/
    enrichment.py    Enricher interface + mock provider
    scoring.py       Fit / intent / engagement scoring with reasons, and tiers
  guardrails/
    policy.py        Autonomy policy: AUTO / NEEDS_APPROVAL / BLOCKED (plain Python)
    gateway.py       The only path from a proposal to a real side effect
    approvals.py     Approval queue for high-risk actions
    audit.py         Append-only audit log
    claims.py        Atomic per-action claims (idempotency)
    untrusted.py     Wraps inbound content as data; detects prompt injection
    kill_switch.py   Emergency stop
segments/            One YAML config pack per customer segment
data/                Mock leads, enrichment, replies, call transcripts, buyer personas, IO template
run_demo.py          The terminal demo
run_world.py         The live world in the foreground, in real time
app.py               The browser UI (streamlit run app.py)
scripts/             check_claude.py, kill_switch.py
tests/
docs/                DESIGN.md (decisions), REVIEW_GUIDE.md (how to review this change)
state/               Runtime state (world, approvals, audit log, claims). Git-ignored.
```

## Adding a customer segment

Add `segments/<name>.yaml` following [`segments/brand.yaml`](segments/brand.yaml) or [`segments/agency.yaml`](segments/agency.yaml). No code changes are needed: the agency segment was added this way. The pack is validated when it loads: weights must add up to 1, tier A must be above tier B, every rule must reference a real field, and Tier A first contact must stay in `autonomy.first_touch_approval_tiers`.

## Kill switch

An emergency **pause** for every agent action. Works on Windows, macOS and Linux:

```bash
python scripts/kill_switch.py on --reason "Bad emails going out"   # pause
python scripts/kill_switch.py status                                # held events, approved-but-waiting actions
python scripts/kill_switch.py off                                   # resume
```

While it is on, **nothing executes and nothing is lost**: incoming events (new leads, replies, calls, signatures) are **held** in arrival order; human approval decisions are still recorded, but the approved actions wait; the daily digest still runs (it only reports). `off` replays held events in order, then runs approved actions that waited. Pause and resume are written to the audit log. Resetting the demo never turns the switch off.

It defaults to the demo state (`state/demo/`); use `--state-dir` for another one. The switch itself is the file `KILL_SWITCH` in that folder, and the gateway checks it before every action as a second layer. In the UI, **Pause all agents** on Today or Team does the same, and **Release** replays what waited.
