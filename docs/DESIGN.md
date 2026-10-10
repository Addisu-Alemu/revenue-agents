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

Each agent has **one job** and returns its findings plus the **actions it proposes**. It never executes them. "Tools" here are the action types the policy allows the agent to propose (`AGENT_PERMISSIONS`). Anything else is blocked.

| Agent | Responsibility | LLM? | May propose |
| --- | --- | --- | --- |
| **Research** | Enrich a lead (firmographics, buying signals) | No | `enrich_lead` |
| **Scoring** | Fit / intent / engagement with reasons → tier | No (deterministic: the tier gates approval) | `score_lead` |
| **Reply triage** | Classify a reply's intent, flag what needs attention, learn a lesson | Yes | `classify_reply`, `flag_deal`, `record_lesson` |
| **Call intel** | Transcript → `DealFields`, pains, objections, lessons | Yes | `update_deal`, `record_lesson` |
| **Outreach** | Draft first-touch email from pack messaging + lessons | Yes | `send_email` |
| **Deal desk** | Generate and validate the IO from deal data; send for approval and e-sign | No | `draft_io`, `send_pricing`, `send_io`, `send_for_signature` |
| **Orchestrator** | Owns deal state and stage transitions; routes events to agents | No | `update_deal`, `flag_deal`, `close_deal` |

**Code checks the LLM can't override:**
- **Reply triage:** pricing requests are flagged by a code-level pattern even if the model misses them. Injection attempts are flagged by `injection_signals()` even if the model is fooled (covered by a test where the fake model *is* fooled). **No lesson is learned from a suspicious message**, so an attacker can't poison the knowledge loop.
- **Call intel:** products must exist in the segment pack (unknown ones are dropped and flagged); flight dates must be in order; missing proposal fields (budget, products, dates, decision-maker) are flagged; extraction says "leave empty, never guess".
- **Outreach:** the model writes only a short opening (≤ 400 chars) and *chooses* a value prop by index. Subject, body structure, call to action and signature come from the pack template, so messaging stays under config control. Phrases the pack forbids (`avoid_phrases`) are rejected in code with whole-word matching, and the agent retries **once** with feedback, then gives up (`DraftRejectedError`) rather than sending. Only lessons that were actually offered can be cited in `lessons_used`, so lesson usage is traceable. Enrichment facts reach the model as untrusted data. Template placeholders are validated when the pack loads.
- **Deal desk:** no LLM. The IO is filled only from validated deal fields; missing or inconsistent data (budget, products in the segment's catalogue, flight dates in order, signatory, payment terms) yields **no draft** and a list of gaps. Any `{{placeholder}}` left unfilled blocks the IO. Line items split the budget evenly as a starting point. Pricing is in the document, so `send_io` is always approved by a human.
- **Idempotent ids:** `<action>:<deal>:<step>` (e.g. `classify_reply:D-001:reply:R-001`), so re-processing the same reply or transcript proposes the same ids, and the gateway runs each once.

**LLM layer (`llm.py`).** Agents depend on a small `LLM` interface (`complete(system, prompt, schema) -> validated model`), not on the SDK.
- **`ClaudeLLM`** runs the Claude Agent SDK **locked down**: `tools=[]` (no built-in file, shell or web tools), `setting_sources=[]` (no developer settings), `max_turns` and a per-call `max_budget_usd` cap, and a JSON-schema `output_format` generated from the pydantic model. Answers are validated, and a malformed answer raises instead of being used. It refuses to run without `ANTHROPIC_API_KEY`, so it never silently bills a logged-in Claude Code account. **The LLM can only return data. Every side effect is a proposed `Action` that goes through the gateway.**
- **`FakeLLM`** returns scripted answers per schema and records every prompt. Tests are offline, free and deterministic, and can assert *what was sent to the model* (e.g. that lessons were included, or that inbound email was wrapped as untrusted).
- **The model is per instance.** Each agent can use the model its job needs (e.g. a cheaper model for classification) via configuration. The default is `claude-opus-5-5`.

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
- **Idempotent, race-safe execution:** each action has a deterministic id (e.g. `email:D-001:first_touch`). Before running, the gateway **claims** the id by creating a marker file with `O_CREAT | O_EXCL`. The OS guarantees only one caller wins, even across processes, so two copies of a webhook arriving at the same moment send **one** email. (A check-then-act version sent 4 emails out of 10 concurrent deliveries; the claim sends 1. Both are covered by tests.) The approval queue is keyed by the same id and guarded by a file lock (`fcntl.flock`). Without it, 19 of 20 concurrent submissions were lost.
- **Inbound content is untrusted** (`guardrails/untrusted.py`). Replies and transcripts reach the model inside `<untrusted source="...">` tags, with a rule never to follow instructions inside them. The content can't close the tag early (escaped), and the source label is sanitised. Separately, a plain-code detector (`injection_signals`) looks for instruction-like text: "ignore previous instructions", fake `[SYSTEM]` notes, "skip approval", "mark as Tier A", requests for internal pricing. *Trade-off:* pattern lists catch known styles, not every attack. They're one layer; the real protection is that the model can't act (no tools, gateway-only side effects) and that risk is read from deal state.
- **Audit log:** append-only JSONL. Every policy decision, execution, duplicate and block is recorded with actor, deal, action and reason.
- **Kill switch:** a file (`state/KILL_SWITCH`). An operator can stop everything without code changes or a restart.
- **Durability:** state is written atomically (unique temp file + rename). If a handler raises, the claim is released and the event can be retried.
- *Trade-off: at-most-once.* If the process **crashes** mid-handler, the claim stays `in_progress` and the action is **not** auto-retried. For emails and contracts, "maybe not sent" (a human can resend) is safer than "sent twice" (a duplicate contract can't be unsent). Stuck claims are listed by `ActionClaims.in_progress()` and surface in stall detection. In production: a database with row-level locks or unique constraints in place of files, plus the action id passed to the provider (email, e-sign) as an idempotency key. File locks are POSIX-only and single-host, which is fine for the slice but not for a multi-host deployment.

## 7. Phased rollout

*(to be completed)*

## 8. Trade-offs and limitations

*(to be completed)*
