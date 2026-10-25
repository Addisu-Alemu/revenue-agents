# Reviewing the agentic redesign

One change on top of `main` at `313eec2`: agents that choose their own tools, a live world instead of a scripted story, and a UI that shows them working. The decisions and trade-offs are in [DESIGN.md](DESIGN.md). This page is the reading order.

## Applying it

The patch is the safe way: it carries the deleted files too.

```bash
git checkout 313eec2            # or a branch from it
git apply --whitespace=nowarn revenue-agents-redesign.patch
pip install -r requirements.txt  # pins claude-agent-sdk 0.2.168 and streamlit 1.66.0
pytest                           # 412 tests, offline
```

If you use the zip instead, unpack it into an **empty** folder, not over an existing checkout. These files were deleted and would otherwise stay behind:

- `revenue_agents/story.py` (now `tests/story.py`: a fixed event order for tests only)
- `revenue_agents/agents/scoring.py` (scoring is a code skill the research agent calls)
- `revenue_agents/agents/reply_triage.py` (now `revenue_agents/agents/reply.py`)
- `revenue_agents/ui/pages/demo_control.py` and `revenue_agents/ui/pages/approvals.py` (now Today and Inbox)
- `tests/test_agent_views.py` (its views were removed; the new ones are covered by `test_ui.py` and `test_feed.py`)

## Reading order

| # | Part | Files | What to look for |
|---|------|-------|------------------|
| 1 | Agents as tool loops, and the hook | `revenue_agents/runtime.py`, `guardrails/gateway.py` (`check`), `guardrails/policy.py` (three new low-risk actions, new agent permissions) · `tests/test_runtime.py` | The hook only maps the policy to allow / defer / deny. An allowed side effect still goes through `gateway.submit`. A deferred call is already in the approval queue, and the run stops. Action ids come from the tool input. `ClaudeRuntime.options`: `tools=[]`, only this agent's tools, `dontAsk`, `setting_sources=[]`, `strict_mcp_config`, turn limit, spend cap, no key means no run. |
| 2 | The four agents | `agents/research.py`, `outreach.py`, `reply.py`, `call_intel.py`, `demo_llm.py` · `tests/test_agents_part_a.py`, `test_agents_part_b.py`, `test_handlers.py`, `tests/desk.py` | Each agent is a task: a system prompt, a prompt, its tools and a `script`, which is how the scripted mode plays it. Rules that a model mustn't decide are checks on the tool. The draft tool hands problems back, so the agent fixes its own email. `demo_llm.py` holds only decisions. |
| 3 | Orchestrator | `orchestrator.py` (`_work`, `_first_email`, `_on_reply`, `_on_call`, `_on_follow_up_due`), `stages.py`, `handlers.py`, `tracking.py` · `tests/test_orchestrator.py`, `test_conversation.py`, `test_io_versions.py`, `test_kill_switch_pause.py` | It sees each side effect as it happens, so stages move at the right moment. Replied now needs a sent email too. Contracts, unsubscribes, signatures and replies from strangers stay in code. A deal waiting for a follow-up the prospect asked for isn't stalled. |
| 4 | The live world | `storage.py` (clock, `try_lock`), `world.py`, `simulator.py`, `events.py`, `data/personas.json`, `data/leads.json` and `enrichment.json` (two late leads), `demo.py`, `run_demo.py`, `run_world.py` · `tests/test_world.py`, `test_demo.py` | Buyers act only on what the agents actually did. Your decisions are delivered first, even while paused. One runner per folder (a lock file); a crash mid-delivery is recovered. A model failure pauses the world and the item stays queued. |
| 5 | The UI | `app.py`, `ui/live.py`, `ui/feed.py`, `ui/agent_views.py`, `ui/components.py`, `ui/pages/today.py`, `inbox.py`, `team.py`, `agent_trace.py`, `revenue_agents/team.py` · `tests/test_ui.py`, `test_feed.py` | Everything shown comes from the trace, the world's log or the code: the Team page reads each agent's real tools and the policy. Decisions go through the world's queue. The kill switch acts at once. |
| 6 | Docs and checks | `README.md`, `docs/DESIGN.md`, `scripts/check_claude.py`, `requirements.txt`, `pyproject.toml` | `check_claude.py --agent` is the live check. |

## Try it

```bash
streamlit run app.py      # press Start; watch Today; approve, reject, or Redraft with a note in the Inbox; pause and release
python run_demo.py        # the same world in the terminal, asking you at each approval
```

In scripted mode, tool calls are paced at 0.5 s so you can follow them, and the UI says the decisions are scripted.

## What was checked, and what wasn't

- **Run:** the full test suite on the work copy and again on a fresh clone with the patch applied (numbers are in the handover note). A browser walk on localhost in scripted mode: start the world; approve Maya's first email in the Inbox; coach Lumen's email with a note kept as a lesson (the agent redrafts it as v2); pause all agents (arrivals wait and are counted); release (they replay); open Team, Agent trace and Deal detail. The terminal demo with auto-approve, end to end through the signature.
- **Not run:** a live Claude run. The real-model path is tested against a stand-in for the SDK's `query()` that calls the hook and the tools the way Claude Code does, but not against the API. One command does it, for about a dollar at most: put `ANTHROPIC_API_KEY` in `.env`, then run `python scripts/check_claude.py --agent`.
- **Not run:** the Docker image. The Dockerfile is unchanged; the image copies the new files the same way.
