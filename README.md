# Revenue Agents

An agentic revenue system for Cool AI: single-purpose AI agents, coordinated by an orchestrator, that move a lead from **prospect → enrich → score → outreach → reply → meeting → qualify → proposal / IO → signature → handoff**. Risky steps wait for a human, and that rule is enforced in code. Built on the Claude Agent SDK, with HubSpot mocked as the system of record.

Design document: https://claude.ai/artifact/NN3rv38qkcyhZjwnuUUYDf

> **Status: complete vertical slice.** Seven agents (research, scoring, outreach, reply triage, call brief, call intel, deal desk) coordinated by an orchestrator, with the autonomy policy, action gateway, approval queue, audit log, kill switch, stage tracking, daily digest and e-signature through to handoff. Two segments: brands, and agencies added through `segments/agency.yaml` alone. Runs in the terminal or in a browser UI, with real Claude or with scripted answers.

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

Check the connection (one tiny request, costs a fraction of a cent):

```bash
python scripts/check_claude.py
```

## Run the demo

```bash
python run_demo.py --llm claude     # real Claude (needs ANTHROPIC_API_KEY with credit in .env)
python run_demo.py                  # scripted model answers, no API key needed
python run_demo.py --auto-approve   # don't pause at approvals
python run_demo.py --segment agency # the agency segment (added with config only)
```

The demo runs the brand segment's mock data through the real system: a new lead is enriched, scored and tiered; the Tier A first email **pauses for your approval**; Maya replies with a question first (can corporate control what franchisees change?), the outreach agent drafts an answer that **pauses for approval** too, and her next reply asks for a meeting, so the deal moves to Meeting and a **call brief** is prepared for the rep (a duplicate webhook is ignored); an intro call leaves budget and dates open, so the rep gets an **updated call brief**, and the call with the CFO fills them in; the calls produce lessons, some for the brand segment and some **global** for every segment; the IO is drafted and **pauses for approval** and goes out for e-signature; Maya then emails that Ken wants Net 45, so the agents draft **IO v2** for your approval (approving sends it and voids the first IO, so only the new one can be signed); a forged "signed" webhook is rejected and the real one closes the deal (handoff); the next brand lead's email **applies a lesson** from the call; Owen's prompt-injection reply is flagged; and six days later the SLA check flags a stalled deal and the daily digest prints. It ends with the pipeline, the knowledge loop and the audit log size. The whole conversation with Maya is stored as one email thread.

Without `--llm claude`, model answers are scripted for the mock data (and the demo says so); everything else is the real code path. `--llm claude` never falls back to scripted answers: if the key is missing or has no credit, it stops with an error. Demo state is written to `state/demo/` and reset on each run (`--keep-state` to keep it).

## UI

```bash
streamlit run app.py
```

Opens in the browser (http://localhost:8501). The **Live demo** page runs the same story one step at a time: each agent appears **live as it works** (with its result, model time and cost), an **agent map** lights up the agents, policy and stores that worked on the last step, and approvals are decided inline (an email can be **edited before approving**: your version is sent, the agent's draft is kept, and the edit is audited); **Agent trace** shows **scorecards for the agent team** (runs, model calls, time, cost, latest result) and, for any event, each agent's input, the exact prompt sent to the model, its raw answer, what was proposed and what the policy decided; **Pipeline**, **Deal detail** (including the email **Conversation**, both ways, and the **Call**: its transcript and what the agent took from it; the **Insertion order** tab lists every IO version with its status and what changed, offers a download, and lets the rep **change the terms**, which redrafts the IO, replaces a pending version and voids a sent one so it can't be signed) and **Approvals** show the state. It shares `state/demo/` with `run_demo.py`, and every button sends an event to the orchestrator, so the policy and gateway apply exactly as in the terminal.

## Tests

```bash
pytest                                                  # all tests, offline and free
pytest --cov=revenue_agents --cov-report=term-missing   # with coverage
```

Tests use `FakeLLM`, which gives scripted answers, so they need no API key or network and give the same result every run.

## Project structure

```
revenue_agents/
  models.py          Shared data models: Lead, Deal, LeadScore, Stage, Tier, ...
  segments.py        Loads and validates segment config packs
  llm.py             LLM interface: ClaudeLLM (Agent SDK, locked down) and FakeLLM (tests)
  crm.py             CRM interface + MockHubSpot (system of record: leads, deals, timeline)
  lessons.py         Lesson store: knowledge loop memory, with usage tracking
  handlers.py        What executed actions actually do (the only side-effecting code)
  agents/            research, scoring, outreach, reply_triage, call_brief, call_intel, deal_desk
  orchestrator.py    Owns deal state; handles events; moves stages
  stages.py          Stage order and exit criteria
  events.py          Event types (triggers)
  system.py          Composition root: build_system() and make_llm()
  tracking.py        Stall detection, reminders, daily digest
  esign.py           E-signature interface + mock provider
  demo.py            The live demo story; demo_llm.py holds the scripted model answers
  storage.py         Atomic, locked file storage for runtime state
  tracing.py         Agent trace: TracedAgent / TracingLLM wrappers record each step per event
  ui/                Browser UI pages: live demo, agent trace, pipeline, deal detail, approvals
  skills/
    enrichment.py    Enricher interface + mock provider
    scoring.py       Fit / intent / engagement scoring with reasons, and tiers
  guardrails/
    policy.py        Autonomy policy: AUTO / NEEDS_APPROVAL / BLOCKED (plain Python)
    gateway.py       The only path from an agent's proposal to a real side effect
    approvals.py     Approval queue for high-risk actions
    audit.py         Append-only audit log
    claims.py        Atomic per-action claims (idempotency)
    untrusted.py     Wraps inbound content as data; detects prompt injection
    kill_switch.py   Emergency stop
segments/            One YAML config pack per customer segment
data/                Mock leads, enrichment, replies, call transcript, IO template
run_demo.py          Entry point for the terminal demo
app.py               Entry point for the browser UI (streamlit run app.py)
scripts/             Other entry points (check_claude.py, kill_switch.py)
tests/
docs/DESIGN.md       Link to the design document
state/               Runtime state (approvals, audit log, claims). Git-ignored.
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

It defaults to the demo state (`state/demo/`); use `--state-dir` for another one. The switch itself is the file `KILL_SWITCH` in that folder, and the gateway checks it before every action as a second layer.
