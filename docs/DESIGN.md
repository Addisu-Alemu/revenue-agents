# Agentic Revenue System: Design

The design document (architecture, agents and tools, skills and segment packs, trigger map, data model, autonomy policy, phased rollout, trade-offs) is here:

https://claude.ai/artifact/NN3rv38qkcyhZjwnuUUYDf

## October redesign: from a scripted story to agents that act

Feedback on the first version: it felt clunky and not agentic. That was fair, for three reasons the code showed:

- **A person drove it.** The UI's main control was a "Next: <step>" button through a fixed story. Nothing happened unless someone clicked.
- **The agents didn't choose.** Each "agent" was one model call that filled in a form (`tools=[]` in `llm.py`). The orchestrator decided every step. Four of the seven agents were plain code, and the trace page said so ("code", 0.00 s, $0.0000).
- **The world didn't react.** Replies arrived in a fixed order, whatever the agents had written or whether the email had even gone out.

What was right stays: the policy in code behind one gateway, idempotent actions and events, the approval queue and audit log, the kill switch that holds and replays, stage criteria, segment packs, lessons, the call brief, IO versions, and the tests. The redesign changes who acts, not what keeps them safe.

### Decisions

1. **Agents are tool loops.** Research, outreach, reply and call intel each get a small set of tools and decide which to call, in what order, and when they're done (`runtime.py`, `agents/`). With Claude, each run is a Claude Agent SDK `query()`. Its tools are an in-process MCP server, there are no built-in tools, only that agent's tools are allowed (`dontAsk`), settings and other MCP servers are ignored, and there is a turn limit and a spend cap.

2. **The policy is a PreToolUse hook in front of the gateway, not instead of it.** The hook answers each call: allow, defer (the run stops and the call waits for a human) or deny (the reason goes back to the model). An allowed side effect still goes through `gateway.submit`, which decides again and claims the action id before anything runs. So the hook can't be the only line, and every existing guarantee (claims, audit, approvals, kill switch) holds unchanged. Action ids come from the tool input (`type:deal:key`), never from the model or the SDK, so a retried event can't act twice.

3. **Approving doesn't resume the model.** A deferred call is already a complete, checked action in the approval queue. Approving runs it through `gateway.execute_approved`, exactly as before. A rejection with a note starts a new run with the note as feedback. Resuming the SDK session would add a stateful path and change nothing about safety.

4. **Rules a model shouldn't decide are code, at the tool.** No drafted answer to a pricing question or a suspicious message. No meeting nobody asked for. No lesson from a message with instructions aimed at an AI. Dates must be in range. A draft must pass the template and forbidden-phrase checks before it can be sent: the agent gets the problems back and fixes its own draft. Each tool declares the one action type it may propose, and the hook refuses anything else.

5. **Some parts stay code on purpose.** Scoring decides who needs a human, so it must be exact and explainable: the research agent calls it as a tool and can't change a number. The deal desk builds contracts from checked fields and a template, because no model should write an IO. The call brief assembles what is known. The UI calls these skills, not agents.

6. **The orchestrator keeps what must be deterministic.** It owns stage moves (each needs evidence), unsubscribes, IO versions and voiding, signature verification, replies from people we never emailed, the SLA check and the digest. It sees each side effect as it happens (`on_result`), so stages move at the right moment. For example, a reply is classified, so the deal is Replied, so an answer drafted next to a top-tier lead waits for a human.

7. **A live world instead of a script.** `world.py` has a clock (play, pause, speed, next arrival), a queue, a scheduler (SLA check every morning, digest every evening) and the timers agents set. `simulator.py` plays each prospect from a hidden persona (`data/personas.json`). A prospect answers only an email that actually went out, joins the call the reply agent booked, asks for the IO change once they have the IO, and signs the one that is still valid. The prospects' replies are the written ones, word for word, so a live run spends its model time on the agents. The simulator can also reword them in the persona's voice against the actual email (a `voice` model, off by default); a reply carrying a prompt injection is always delivered word for word. While the world runs, its clock is the system clock, so stages, approvals, stall checks and the audit log agree.

8. **One queue, one runner.** Your decisions go into the world's queue and are delivered first, even while the clock is paused. The kill switch is the exception: it acts at once. One runner per state folder delivers everything (a lock file enforces it), so you and the agents never work on the same deal at the same moment. A delivery that fails because the model failed pauses the world and stays queued; Play retries it.

9. **The UI shows the agents working, from the trace.** Today shows what needs you, who is working right now (tool calls appear as they're made), what happened (each arrival with its agent runs and their tool calls), and what is coming up. The Inbox shows why each item stopped, the agent's tool trail and the evidence it used, and lets you coach a redraft. The Team page is generated from the code: each tool's permission comes from the policy and the packs. Scripted mode paces tool calls (0.5 s) so a person can follow them, and says it's scripted.

10. **Streamlit stays.** A FastAPI and React front end with server-sent events would stream more smoothly. But it would rewrite the UI without making anything more agentic. Fragments that refresh every second, fed by a background runner, are enough to show a live desk.

### Not done in this round

- **Temporal.** A workflow per deal (signals for replies and decisions, timers for follow-ups, side effects on a separate worker) would make the world durable across machines. It makes the system sturdier, not more agentic. The file-based world already survives restarts.
- **A goal-directed copilot** ("follow up with every deal that went quiet after a pricing question") that plans across deals with read tools and delegates to the agents.
- **Recorded replay** of a live Claude run. Scripted mode is the offline safety net.
- **Real integrations.** HubSpot, the inbox, the calendar and e-signature stay mocked behind their interfaces.

### Trade-offs to know

- With Claude, the agents can choose differently from the scripted run: book instead of answer, or write a different lesson. The world reacts to whatever they did. The rules don't move.
- The scripted runtime mirrors what a sensible model would do. It tests the tools, the hook, the policy and the flow, not the model's judgement. `python scripts/check_claude.py --agent` does one real research and outreach run and checks that the Tier A email stopped for a human.
- Two late mock leads (Crumb & Co, Northwind) arrive after the BrightBite calls, so the learning loop shows in later emails, including a global lesson crossing into the agency segment.
