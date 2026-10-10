# Revenue Agents

An agentic revenue system for Cool AI: single-purpose AI agents, coordinated by an orchestrator, that move a lead from **prospect → enrich → score → outreach → reply → meeting → qualify → proposal / IO → signature → handoff**. Risky steps wait for a human, and that rule is enforced in code. Built on the Claude Agent SDK, with HubSpot mocked as the system of record.

Design: [docs/DESIGN.md](docs/DESIGN.md)

> **Status: in progress.** Built so far: the full vertical slice for the brand segment (data models, segment config packs, enrichment and scoring, the autonomy policy with action gateway, approval queue, audit log and kill switch, the LLM layer, all six agents, mock HubSpot, the lesson store, the orchestrator with stage exit criteria, and the demo run), plus a second segment, agencies, added through `segments/agency.yaml` alone. It also covers tracking (stall detection, reminders, daily digest) and e-signature through to handoff. Next: the UI.

## Setup

Requires **Python 3.10+** (developed on 3.11) and an **Anthropic API key**.

```bash
git clone https://github.com/Addisu-Alemu/revenue-agents.git
cd revenue-agents

# Create and activate a virtual environment (uv shown; `python3.11 -m venv .venv` works too)
uv venv -p 3.11 .venv
source .venv/bin/activate

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
python run_demo.py                  # fake model: scripted answers, no API key needed
python run_demo.py --llm claude     # real Claude (needs ANTHROPIC_API_KEY with credit in .env)
python run_demo.py --auto-approve   # don't pause at approvals
python run_demo.py --segment agency # the agency segment (added with config only)
```

The demo runs the brand segment's mock data through the real system: a new lead is enriched, scored and tiered; the Tier A first email **pauses for your approval**; Maya's reply moves the deal to a meeting (and a duplicate webhook is ignored); the call transcript fills the deal fields and produces lessons; the IO is drafted and **pauses for approval**, goes out for e-signature, a forged "signed" webhook is rejected and the real one closes the deal (handoff); the next brand lead's email **cites the lesson** from the call; Owen's prompt-injection reply is flagged; and six days later the SLA check flags a stalled deal and the daily digest prints. It ends with the pipeline, the knowledge loop and the audit log size.

In fake mode, model answers are scripted for the mock data (and the demo says so); everything else is the real code path. `--llm claude` never falls back to fake: if the key is missing or has no credit, it stops with an error. Demo state is written to `state/demo/` and reset on each run (`--keep-state` to keep it).

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
  agents/            research, scoring, outreach, reply_triage, call_intel, deal_desk
  orchestrator.py    Owns deal state; handles events; moves stages
  stages.py          Stage order and exit criteria
  events.py          Event types (triggers)
  system.py          Composition root: build_system() and make_llm()
  tracking.py        Stall detection, reminders, daily digest
  esign.py           E-signature interface + mock provider
  demo.py            The live demo story; demo_llm.py holds the scripted model answers
  storage.py         Atomic, locked file storage for runtime state
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
run_demo.py          Entry point for the live demo
scripts/             Other entry points (check_claude.py)
tests/
docs/DESIGN.md
state/               Runtime state (approvals, audit log, claims). Git-ignored.
```

## Adding a customer segment

Add `segments/<name>.yaml` following [`segments/brand.yaml`](segments/brand.yaml) or [`segments/agency.yaml`](segments/agency.yaml). No code changes are needed: the agency segment was added this way. The pack is validated when it loads: weights must add up to 1, tier A must be above tier B, every rule must reference a real field, and Tier A first contact must stay in `autonomy.first_touch_approval_tiers`.

## Kill switch

```bash
touch state/KILL_SWITCH    # stop every agent action, including already-approved ones
rm state/KILL_SWITCH       # resume
```
