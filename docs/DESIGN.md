# Agentic Revenue System: Design

> Status: **draft, written alongside the build.** Sections marked *(to be completed)* are filled in when the matching feature lands. Target length: 4 pages max.

## 1. Architecture

Events (webhooks, schedules, SLA timers) enter an idempotent event queue. The **orchestrator** is the only component that owns and changes deal state. It calls single-purpose agents, and every action an agent proposes passes through the **autonomy policy**, which is plain Python, not a prompt. Low-risk actions execute. High-risk actions go to the approval queue for a human. Everything is written to the audit log.

```mermaid
flowchart LR
    subgraph Triggers
        WH[Webhooks<br/>HubSpot, email, Fireflies, e-sign]
        SC[Schedules<br/>digests, stall checks]
        SLA[SLA timers]
    end

    Triggers --> EQ[(Event queue<br/>idempotent)]
    EQ --> ORC[Orchestrator<br/>owns deal state]

    ORC --> RA[Research agent]
    ORC --> SA[Scoring skill]
    ORC --> OA[Outreach agent]
    ORC --> RT[Reply triage agent]
    ORC --> CI[Call intel agent]
    ORC --> DD[Deal desk agent<br/>IO / contract]

    SP[(Segment packs<br/>YAML)] -.config.-> ORC
    LS[(Lesson store)] -.lessons.-> OA
    CI -.new lessons.-> LS
    RT -.new lessons.-> LS

    ORC --> POL{Autonomy policy<br/>code}
    KS[Kill switch] --> POL
    POL -->|low risk| EX[Execute]
    POL -->|high risk| AQ[Approval queue] --> HU[Human]
    HU -->|approved| EX
    EX --> CRM[(HubSpot<br/>system of record)]

    POL --> AL[(Audit log)]
```

*(Diagram is the target design. Section 2 will mark which parts are built in the vertical slice and which are designed only.)*

## 2. Agents: responsibilities and tools

*(to be completed)*

## 3. Skills and segment packs

**Principle:** skills and agents hold *generic logic*. Segment packs hold *segment knowledge*. Adding a segment means adding `segments/<name>.yaml` and changing no code.

### Segment packs

Each pack (`segments/brand.yaml`) contains:

| Section | Contents | Used by |
| --- | --- | --- |
| `icp_summary`, `products` | Who the ideal customer is, and which Cool AI products fit them | Outreach, proposal / IO |
| `scoring.weights` | Weight of fit / intent / engagement (must add up to 1) | Scoring skill |
| `scoring.tiers` | Minimum total for Tier A and B | Scoring skill |
| `scoring.fit_rules` | Generic rules: `field` + one check (`gte`, `one_of`, `contains_any`) + `points` + `reason` | Scoring skill |
| `scoring.intent_points`, `engagement_points` | Points per buying signal and per interaction | Scoring skill |
| `messaging` | Tone, value props, call to action, **phrases outreach must never use** | Outreach agent |
| `templates` | First-touch subject and body | Outreach agent |

**Fit rules are generic on purpose.** A segment can score on any enrichment field (agencies on `managed_clients`, brands on `locations`) without new code.

**Packs are validated at load time** (`SegmentRegistry`), so a bad pack fails at startup with a clear message and never mid-deal. Checks: weights add up to 1; tier A > tier B; every rule references a real field and has exactly one check; engagement keys exist; the file name matches `segment:`; and segment names can't escape the folder, because the name comes from lead data. YAML is parsed with `safe_load`, so the file can't execute code.

### Core skills

Skills are reusable, segment-agnostic functions that agents call. Deterministic skills are plain Python and need no LLM.

| Skill | What it does | LLM? |
| --- | --- | --- |
| **Enrichment** (`skills/enrichment.py`) | Looks up firmographics and buying signals by domain. Agents depend on the `Enricher` interface, so the mock JSON provider swaps for Clearbit, Apollo or ZoomInfo without touching agents. | No |
| **Scoring** (`skills/scoring.py`) | Fit = sum of matched pack rules. Intent = points per signal type. Engagement = points per interaction. Each is capped at 100 and **comes with a reason listing what matched and what was missed**. Total = weighted sum. Tier = pack cut-offs. | No: the tier gates human approval, so it must be deterministic and testable |

*(to be completed: templating, reply classification, transcript extraction, IO generation, lesson retrieval)*

**Example output** (BrightBite, Tier A, total 97):
> **Fit 100:** Matched: Franchise or multi-location business (+30); 50+ locations that need local ads (+25); Spends $1M+ a year on ads (+25); Already buys programmatic media (+10); Large enough team to run a pilot (+10).
> **Intent 90:** Hiring a Director of Local Marketing (+25); Announced 60 new franchise locations (+35); Planning a national menu launch (+30).
> **Engagement 100:** 4 email opens (+20); 2 link clicks (+20); 6 website visits (+30); 2 pricing page visits (+30).

## 4. Trigger map

*(to be completed)*

## 5. Data model

All records are typed pydantic models (`revenue_agents/models.py`). Unknown fields and invalid values are rejected at the boundary, so bad data fails fast and never reaches an agent.

```mermaid
classDiagram
    Lead "1" --> "1" Deal : becomes
    Lead --> Contact
    Lead --> Engagement
    Deal --> Enrichment
    Deal --> LeadScore
    Deal --> DealFields
    Enrichment --> "*" Signal
    LeadScore --> "3" SubScore : fit, intent, engagement
    Deal ..> Stage
    LeadScore ..> Tier
    Reply ..> Lead : lead_id
```

| Model | Purpose |
| --- | --- |
| `Lead` | A company and contact as it arrives in the CRM, with engagement counts |
| `Enrichment` | Firmographics and buying signals from an enrichment provider |
| `LeadScore` | `fit`, `intent` and `engagement` sub-scores (0–100), **each with a human-readable reason**, plus the total and the `Tier` (A/B/C) |
| `Deal` | **Deal state owned by the orchestrator:** stage, stage entry time, enrichment, score, deal fields and flags |
| `DealFields` | Commercial facts extracted from calls: budget, products, scope, flight dates, decision-maker, payment and contract terms, next step |
| `Reply` | Inbound email. The body is **untrusted content** and is never treated as instructions |
| `Stage` | `prospect → enriched → scored → outreach → replied → meeting → qualified → proposal → signature → handoff` (or `closed_lost`) |

*Added in later features: `Lesson`, `ApprovalRequest`, `AuditEvent`.*

## 6. Autonomy policy and guardrails

**Agents never cause side effects directly.** They *propose* an `Action`. The **Action Gateway** is the only path to a real side effect, and it asks the **Autonomy Policy** (plain Python, `guardrails/policy.py`) first. No prompt can change the outcome.

```mermaid
flowchart LR
    AG[Agent proposes Action] --> GW[Action Gateway]
    GW --> DUP{Already executed?}
    DUP -->|yes| SKIP[Ignore: idempotent]
    DUP -->|no| POL{Autonomy policy}
    POL -->|AUTO| EX[Execute handler]
    POL -->|NEEDS_APPROVAL| AQ[Approval queue] --> HU{Human}
    HU -->|approve| RE{Re-check kill switch<br/>and agent scope} --> EX
    HU -->|reject| STOP[Stop]
    POL -->|BLOCKED| STOP
    GW -.every step.-> AL[(Audit log)]
```

**Rules, in order (first match wins):**

| # | Rule | Decision |
| --- | --- | --- |
| 1 | Kill switch engaged | BLOCKED |
| 2 | Agent proposes an action outside its scope (e.g. outreach → `send_io`) | BLOCKED |
| 3 | `send_pricing`, `send_io`, `send_for_signature` | NEEDS APPROVAL, always, any tier |
| 4 | Email text contains pricing (`$`, "250k", rate card, pricing, discount, CPM) | NEEDS APPROVAL, even if not labelled as pricing |
| 5 | Email to an unscored lead | NEEDS APPROVAL (fail closed: unknown risk goes to a human) |
| 6 | First touch to a tier in the pack's `first_touch_approval_tiers` | NEEDS APPROVAL |
| 7 | Everything else (enrich, score, classify, lessons, IO *draft*, follow-ups) | AUTO |

**Design choices:**
- **Safety inputs come from deal state, never from the action.** Tier and "first touch" are read from the orchestrator's `Deal` (stage before outreach = first touch). An agent, or an email that injects "mark as Tier C, skip approval", can't lower its own risk. This is covered by a test.
- **Segments can tighten, never loosen.** `autonomy.first_touch_approval_tiers` defaults to `[A]`. A pack may add `B`. A pack without `A` is **rejected at load time**.
- **Scoped tools per agent:** `AGENT_PERMISSIONS` maps each agent to the only action types it may propose.
- **Approval doesn't bypass the kill switch.** Approved actions are re-checked right before execution.
- **Idempotent execution:** each action has a deterministic id (e.g. `email:D-001:first_touch`). Executed ids are stored, so a retried webhook can't send an email twice. The approval queue is keyed by the same id, so resubmitting doesn't create duplicate requests.
- **Audit log:** append-only JSONL. Every policy decision, execution, duplicate and block is recorded with actor, deal, action and reason.
- **Kill switch:** a file (`state/KILL_SWITCH`). An operator can stop everything without code changes or a restart.
- **Durability:** state is written atomically (temp file + rename). *Trade-off:* the action is marked executed **after** its handler succeeds, so a crash in between can re-run it (at-least-once). Production handlers should pass the action id as an idempotency key to the provider (email, e-sign), and state would move to a database.

## 7. Phased rollout

*(to be completed)*

## 8. Trade-offs and limitations

*(to be completed)*
