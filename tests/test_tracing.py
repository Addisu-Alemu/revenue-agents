"""The agent trace records what each agent received, asked the model, got back and proposed, per event."""

import pytest
from claude_agent_sdk import ResultMessage

from revenue_agents.demo_llm import demo_llm
from revenue_agents.llm import ClaudeLLM, FakeLLM, LLMError
from revenue_agents.models import Model
from revenue_agents.runtime import ScriptedRuntime
from revenue_agents.system import build_system
from revenue_agents.tracing import NullTracer, TracedAgent, Tracer, TracingLLM, describe
from tests import story


@pytest.fixture
def system(tmp_path):
    return build_system(ScriptedRuntime(demo_llm()), state_dir=tmp_path)


def kinds(spans):
    return [(span.kind, span.name) for span in spans]


def test_a_new_lead_is_traced_step_by_step(system):
    story.run(system, story.plan(system.crm.leads(), "brand")[0])
    spans = system.tracer.spans()

    assert {span.event_id for span in spans} == {"lead:L-001"}
    assert kinds(spans) == [
        ("run", "research"), ("tool", "read_lead"),
        ("policy", "enrich_lead"), ("action", "enrich_lead"), ("tool", "enrich_company"),
        ("policy", "score_lead"), ("action", "score_lead"), ("tool", "score_lead"),
        ("policy", "record_research"), ("action", "record_research"), ("tool", "save_research_note"),
        ("agent", "research"), ("stage", "enriched"), ("stage", "scored"),
        ("run", "outreach"), ("tool", "read_account"), ("tool", "search_lessons"), ("tool", "draft_email"),
        ("policy", "send_email"), ("tool", "send_email"), ("agent", "outreach"),
    ]
    send = next(span for span in spans if span.kind == "tool" and span.name == "send_email").data
    assert (send["verdict"], send["rule"], send["outcome"]) == ("defer", "first_touch_tier", "queued")


def test_agent_runs_record_the_model_prompt_and_status(system):
    story.run(system, story.plan(system.crm.leads(), "brand")[0])
    outreach = next(span for span in system.tracer.spans() if span.kind == "agent" and span.name == "outreach")

    assert outreach.data["model"] == "scripted answers"
    assert outreach.data["status"] == "waiting_for_approval"
    assert outreach.data["tools"] == ["read_account", "search_lessons", "draft_email", "send_email"]
    assert "Company: BrightBite Burgers" in outreach.data["prompt"]
    assert "Don't start with a greeting" in outreach.data["system"]
    assert "send it" in outreach.data["system"]


def test_tool_calls_record_input_verdict_and_result(system):
    story.run(system, story.plan(system.crm.leads(), "brand")[0])
    tools = {span.name: span.data for span in system.tracer.spans() if span.kind == "tool"}

    assert tools["enrich_company"]["action_id"] == "enrich_lead:D-001:enrich"
    assert (tools["enrich_company"]["verdict"], tools["enrich_company"]["outcome"]) == ("allow", "executed")
    assert "420 locations" in tools["enrich_company"]["result"]
    assert tools["draft_email"]["input"]["value_prop_index"] == 0
    assert tools["read_lead"]["rule"] == "read_only"


def test_stage_that_does_not_move_is_explained(tmp_path):
    partial = {"deal_fields": {"next_step": "Follow up"}, "lessons": []}
    llm = demo_llm()
    system = build_system(ScriptedRuntime(FakeLLM({**llm.responses, "CallDigest": partial})), state_dir=tmp_path)
    system.orchestrator.handle(story.lead_event(system.crm.get_lead("L-001")))
    system.orchestrator.handle(story.call_event(system.crm.get_lead("L-001"),
                                                story.load_transcripts()["L-001"][0]))
    stays = [span for span in system.tracer.spans() if span.kind == "stage" and "not moved" in span.data["why"]]
    assert stays[0].data["why"].startswith("not moved: qualified needs budget_usd")


def test_run_now_is_traced_under_its_own_name(system):
    system.orchestrator.handle(story.lead_event(system.crm.get_lead("L-001")))
    system.kill_switch.engage("x")
    system.orchestrator.handle(story.approval_event("send_email:D-001:first_touch", True, "you"))
    system.kill_switch.release()
    system.orchestrator.run_approved("send_email:D-001:first_touch")
    assert "run_now:send_email:D-001:first_touch" in {span.event_id for span in system.tracer.spans()}


def test_claude_calls_record_model_and_cost(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")

    class Answer(Model):
        label: str

    async def query(*, prompt, options):
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                            session_id="s", total_cost_usd=0.0042, structured_output={"label": "ok"})

    tracer = Tracer(tmp_path / "traces.jsonl")
    TracingLLM(ClaudeLLM("claude-opus-5-5", query_fn=query), tracer).complete(system="s", prompt="p", schema=Answer)
    [call] = tracer.spans()
    assert (call.data["model"], call.data["cost_usd"]) == ("claude-opus-5-5", 0.0042)


def test_failures_are_traced_too(tmp_path):
    tracer = Tracer(tmp_path / "traces.jsonl")

    class Answer(Model):
        label: str

    with pytest.raises(LLMError):
        TracingLLM(FakeLLM({}), tracer).complete(system="s", prompt="p", schema=Answer)

    class Broken:
        name = "broken"

        def run(self, *args):
            raise ValueError("boom")

    with pytest.raises(ValueError):
        TracedAgent(Broken(), tracer).run("x")
    assert [span.data["error"] for span in tracer.spans()] == [
        "LLMError: FakeLLM has no scripted answer for Answer", "ValueError: boom"]


def test_wrapped_agents_still_expose_their_attributes(system):
    assert system.orchestrator._agents.research.name == "research"


def test_descriptions_are_short():
    assert describe("x" * 500) == "text (500 characters)"
    assert describe([]) == "0 item(s)"
    assert describe("short") == "short"


def test_null_tracer_records_nothing():
    tracer = NullTracer()
    tracer.record("agent", "x", {})
    assert tracer.spans() == [] and tracer.count() == 0
