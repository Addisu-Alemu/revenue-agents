"""The LLM layer: locked-down options, validated answers, and a fake for tests. No real API calls."""

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
from pydantic import BaseModel

from revenue_agents.llm import ClaudeLLM, FakeLLM, LLMError
from revenue_agents.models import Model


class Answer(Model):
    label: str
    confidence: float


def result(**overrides):
    fields = dict(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                  session_id="s", total_cost_usd=0.001)
    return ResultMessage(**{**fields, **overrides})


def fake_query(*messages):
    """Stands in for claude_agent_sdk.query and records the options it was given."""
    seen = {}

    async def query(*, prompt, options):
        seen["prompt"], seen["options"] = prompt, options
        for message in messages:
            yield message

    return query, seen


@pytest.fixture
def api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")


# --- ClaudeLLM -------------------------------------------------------------------


def test_options_lock_the_model_down():
    options = ClaudeLLM("claude-opus-5-5", max_turns=3, max_budget_usd=0.25).options("Be brief.", Answer)
    assert options.tools == []  # no files, shell or web
    assert options.allowed_tools == []
    assert options.setting_sources == []  # personal settings are not loaded
    assert options.max_turns == 3
    assert options.max_budget_usd == 0.25
    assert options.model == "claude-opus-5-5"
    assert options.output_format["schema"]["additionalProperties"] is False


def test_structured_output_is_validated(api_key):
    query, seen = fake_query(result(structured_output={"label": "interested", "confidence": 0.9}))
    llm = ClaudeLLM(query_fn=query)

    answer = llm.complete(system="Classify.", prompt="Sounds great, let's talk!", schema=Answer)

    assert answer == Answer(label="interested", confidence=0.9)
    assert seen["prompt"] == "Sounds great, let's talk!"
    assert seen["options"].system_prompt == "Classify."
    assert llm.last_cost_usd == 0.001


@pytest.mark.parametrize(
    "text", ['{"label": "x", "confidence": 1}', 'Here you go:\n```json\n{"label": "x", "confidence": 1}\n```']
)
def test_falls_back_to_json_in_text(api_key, text):
    assistant = AssistantMessage(content=[TextBlock(text=text)], model="claude-opus-5-5")
    query, _ = fake_query(assistant, result(result=text))
    assert ClaudeLLM(query_fn=query).complete(system="", prompt="", schema=Answer).label == "x"


@pytest.mark.parametrize(
    ("message", "error"),
    [
        (result(is_error=True, subtype="error_max_budget_usd"), "error_max_budget_usd"),
        (result(structured_output={"label": "x"}), "doesn't match Answer"),
        (result(result="I can't answer that."), "not valid JSON"),
        (result(), "no answer"),
    ],
)
def test_bad_results_raise_clear_errors(api_key, message, error):
    query, _ = fake_query(message)
    with pytest.raises(LLMError, match=error):
        ClaudeLLM(query_fn=query).complete(system="", prompt="", schema=Answer)


def test_sdk_exceptions_become_clear_llm_errors(api_key):
    """E.g. an account with no credit: the SDK raises, we turn it into one readable error."""
    from claude_agent_sdk import ProcessError

    async def failing_query(*, prompt, options):
        raise ProcessError("Claude Code returned an error result: Credit balance is too low")
        yield  # makes this an async generator

    with pytest.raises(LLMError, match="Credit balance is too low"):
        ClaudeLLM(query_fn=failing_query).complete(system="", prompt="", schema=Answer)


def test_no_result_message_raises(api_key):
    query, _ = fake_query()
    with pytest.raises(LLMError, match="no result"):
        ClaudeLLM(query_fn=query).complete(system="", prompt="", schema=Answer)


def test_refuses_to_run_without_api_key(monkeypatch):
    """Never silently fall back to whatever account Claude Code is logged into."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    query, seen = fake_query(result(structured_output={"label": "x", "confidence": 1}))
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        ClaudeLLM(query_fn=query).complete(system="", prompt="", schema=Answer)
    assert seen == {}  # the model was never called


# --- FakeLLM ---------------------------------------------------------------------


def test_fake_llm_answers_by_schema_and_records_calls():
    fake = FakeLLM({"Answer": {"label": "interested", "confidence": 0.8}})
    answer = fake.complete(system="sys", prompt="hello", schema=Answer)

    assert answer.label == "interested"
    assert fake.calls[0].prompt == "hello"
    assert fake.calls[0].schema == "Answer"


def test_fake_llm_can_answer_from_the_prompt():
    fake = FakeLLM({"Answer": lambda call: {"label": call.prompt.upper(), "confidence": 1}})
    assert fake.complete(system="", prompt="hi", schema=Answer).label == "HI"


def test_fake_llm_validates_like_the_real_one():
    with pytest.raises(LLMError, match="doesn't match"):
        FakeLLM({"Answer": {"label": "x"}}).complete(system="", prompt="", schema=Answer)


def test_fake_llm_without_script_raises():
    class Other(BaseModel):
        x: int

    with pytest.raises(LLMError, match="no scripted answer for Other"):
        FakeLLM({}).complete(system="", prompt="", schema=Other)


def test_real_sdk_query_is_used_by_default():
    """Checks the wiring only: nothing is called."""
    import claude_agent_sdk

    from revenue_agents.llm import _sdk_query

    assert _sdk_query() is claude_agent_sdk.query
