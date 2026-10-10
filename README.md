# Revenue Agents

An agentic revenue system for Cool AI: single-purpose AI agents, coordinated by an orchestrator, that move a lead from **prospect → enrich → score → outreach → reply → meeting → qualify → proposal / IO → signature → handoff**. Risky steps wait for a human, and that rule is enforced in code. Built on the Claude Agent SDK, with HubSpot mocked as the system of record.

Design: [docs/DESIGN.md](docs/DESIGN.md)

> **Status: in progress.** Built so far: data models, segment config packs, enrichment and scoring, the autonomy policy (action gateway, approval queue, audit log, kill switch), the LLM layer, all six agents, mock HubSpot, the lesson store and action handlers. The orchestrator, demo run and UI come next.

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
scripts/             Entry-point scripts (e.g. check_claude.py)
tests/
docs/DESIGN.md
state/               Runtime state (approvals, audit log, claims). Git-ignored.
```

## Adding a customer segment

Add `segments/<name>.yaml` following [`segments/brand.yaml`](segments/brand.yaml). No code changes are needed. The pack is validated when it loads: weights must add up to 1, tier A must be above tier B, every rule must reference a real field, and Tier A first contact must stay in `autonomy.first_touch_approval_tiers`.

## Kill switch

```bash
touch state/KILL_SWITCH    # stop every agent action, including already-approved ones
rm state/KILL_SWITCH       # resume
```
