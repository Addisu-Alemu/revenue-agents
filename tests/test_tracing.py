"""The agent trace records what each agent received, asked the model, got back and proposed, per event."""

import pytest
from claude_agent_sdk import ResultMessage

from revenue_agents import story
from revenue_agents.demo_llm import demo_llm
from revenue_agents.llm import ClaudeLLM, FakeLLM, LLMError
from revenue_agents.models import Model
from revenue_agents.system import build_system
from revenue_agents.tracing import NullTracer, TracedAgent, Tracer, TracingLLM, describe


@pytest.fixture
def system(tmp_path):
    return build_system(demo_llm(), state_dir=tmp_path)


def kinds(spans):
    return [(span.kind, span.name) for span in spans]


def test_a_new_lead_is_traced_step_by_step(system):
    story.run(system, story.plan(system.crm.leads(), "brand")[0])
    spans = system.tracer.spans()

    assert {span.event_id for span in spans} == {"lead:L-001"}
    assert kinds(spans) == [
        ("agent", "research"), ("policy", "enrich_lead"), ("action", "enrich_lead"), ("stage", "enriched"),
        ("agent", "scoring"), ("policy", "score_lead"), ("action", "score_lead"), ("stage", "scored"),
        ("llm", "OutreachDraft"), ("agent", "outreach"), ("policy", "send_email"),
    ]
    send = spans[-1].data
    assert (send["decision"], send["rule"]) == ("needs_approval", "first_touch_tier")


def test_model_calls_record_prompt_answer_and_model(system):
    story.run(system, story.plan(system.crm.leads(), "brand")[0])
    [call] = [span for span in system.tracer.spans() if span.kind == "llm"]

    assert call.data["model"] == "scripted answers (fake mode)"
    assert "Company: BrightBite Burgers" in call.data["prompt"]
    assert "Don't start with a greeting" in call.data["system"]
    assert call.data["answer"]["value_prop_index"] == 0


def test_agent_steps_record_inputs_output_and_proposals(system):
    story.run(system, story.plan(system.crm.leads(), "brand")[0])
    research = next(span for span in system.tracer.spans() if span.name == "research")

    assert research.data["inputs"] == ["Lead L-001 (BrightBite Burgers)", "Deal D-001 (prospect)"]
    assert research.data["output"]["locations"] == 420
    assert research.data["proposed"] == ["enrich_lead (enrich_lead:D-001:enrich)"]


def test_stage_that_does_not_move_is_explained(tmp_path):
    partial = {"deal_fields": {"next_step": "Follow up"}, "lessons": []}
    llm = demo_llm()
    system = build_system(FakeLLM({**llm.responses, "CallDigest": partial}), state_dir=tmp_path)
    system.orchestrator.handle(story.lead_event(system.crm.get_lead("L-001")))
    system.orchestrator.handle(story.call_event(system.crm.get_lead("L-001"),
                                                story.load_transcripts()["L-001"]))
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
