"""Providers: scripted replay and the live Anthropic adapter (against a fake SDK client)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from agentic_sdlc.models import AgentOutputError, PermanentError, TransientError
from agentic_sdlc.providers import Playbook, ProviderRequest, ScriptedProvider
from agentic_sdlc.providers.anthropic_provider import AnthropicProvider
from agentic_sdlc.providers.openrouter_provider import OpenRouterProvider, strict_schema
from agentic_sdlc.schemas import ChangeSet, Plan, RequirementSpec

from .conftest import PLAN


def request(node_id: str = "planning", schema=Plan, invocation: int = 0) -> ProviderRequest:
    return ProviderRequest("planner", node_id, "be a planner", {"k": "v"}, schema, invocation)


# ---------------------------------------------------------------- scripted


@pytest.fixture
def playbook(tmp_path) -> Playbook:
    (tmp_path / "responses").mkdir()
    (tmp_path / "responses" / "plan.json").write_text(json.dumps(PLAN))
    change = tmp_path / "changes" / "T1"
    (change / "app").mkdir(parents=True)
    (change / "app" / "new.py").write_bytes(b"X = 1\r\n")  # as a CRLF checkout would have it
    (change / "app" / "old.py.edit").write_text("<<<<<<< SEARCH\na\n=======\nb\n>>>>>>> REPLACE\n")
    (change / "app" / "dead.py.delete").write_text("")
    (tmp_path / "requirement.md").write_text("Do it.\n")
    config = {
        "name": "demo",
        "requirement_file": "requirement.md",
        "responses": {
            "planning": ["responses/plan.json", {"summary": "v2", "test_strategy": "t", "tasks": []}],
            "impl:T1": [{"changeset": "changes/T1", "rationale": "because"}],
            "architecture": [{"not": "a design"}],
        },
    }
    (tmp_path / "playbook.json").write_text(json.dumps(config))
    return Playbook(tmp_path)


def test_scripted_provider_replays_the_sequence_for_a_node(playbook):
    provider = ScriptedProvider(playbook)

    assert provider.generate(request(invocation=0)).output.tasks[0].id == "T1"
    assert provider.generate(request(invocation=1)).output.summary == "v2"
    assert provider.generate(request(invocation=7)).output.summary == "v2"  # the last response repeats
    assert playbook.requirement == "Do it."


def test_scripted_changeset_directory_becomes_a_change_set(playbook):
    output = ScriptedProvider(playbook).generate(request("impl:T1", ChangeSet)).output

    by_path = {change.path: change for change in output.changes}
    assert by_path["app/new.py"].content == "X = 1\n"  # line endings normalised
    assert by_path["app/old.py"].action == "edit" and by_path["app/old.py"].edits[0].replace == "b"
    assert by_path["app/dead.py"].action == "delete"
    assert output.rationale == "because"


def test_scripted_provider_reports_missing_and_invalid_recordings(playbook):
    with pytest.raises(PermanentError, match="no recorded response"):
        ScriptedProvider(playbook).generate(request("verification"))
    with pytest.raises(AgentOutputError, match="violates its schema"):
        ScriptedProvider(playbook).generate(request("architecture"))


# ---------------------------------------------------------------- anthropic (fake client)


class FakeMessages:
    def __init__(self, outcome):
        self.outcome, self.kwargs = outcome, None

    def parse(self, **kwargs):
        self.kwargs = kwargs
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def provider_returning(outcome) -> tuple[AnthropicProvider, FakeMessages]:
    messages = FakeMessages(outcome)
    return AnthropicProvider(client=SimpleNamespace(messages=messages)), messages


def response(stop_reason: str = "end_turn", parsed=None):
    return SimpleNamespace(
        stop_reason=stop_reason,
        parsed_output=parsed,
        model="claude-opus-5-5",
        usage=SimpleNamespace(input_tokens=120, output_tokens=45),
    )


def api_error(cls, status: int):
    http = httpx.Response(status, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    return cls("boom", response=http, body=None)


def test_live_provider_requests_schema_constrained_output():
    provider, messages = provider_returning(response(parsed=Plan.model_validate(PLAN)))

    result = provider.generate(request())

    assert result.output.tasks[2].depends_on == ["T1", "T2"]
    assert result.usage == {"input_tokens": 120, "output_tokens": 45}
    assert messages.kwargs["model"] == "claude-opus-5-5"
    assert messages.kwargs["output_format"] is Plan  # structured output, never free text
    assert messages.kwargs["system"] == "be a planner"
    assert '"k": "v"' in messages.kwargs["messages"][0]["content"]


@pytest.mark.parametrize(
    "error, expected",
    [
        (api_error(anthropic.RateLimitError, 429), TransientError),
        (api_error(anthropic.InternalServerError, 500), TransientError),
        (anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com")), TransientError),
        (api_error(anthropic.BadRequestError, 400), PermanentError),
        (api_error(anthropic.AuthenticationError, 401), PermanentError),
    ],
)
def test_live_provider_maps_sdk_errors_onto_the_retry_taxonomy(error, expected):
    provider, _ = provider_returning(error)
    with pytest.raises(expected):
        provider.generate(request())


# ---------------------------------------------------------------- openrouter (mock transport)


def openrouter(*replies: httpx.Response, waits: list | None = None) -> tuple[OpenRouterProvider, list[dict]]:
    sent: list[dict] = []
    queue = list(replies)

    def handler(http_request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(http_request.content))
        return queue.pop(0)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    sleeps = waits if waits is not None else []
    return OpenRouterProvider(api_key="test-key", client=client, sleep=sleeps.append), sent


def completion(content: str, finish_reason: str = "stop") -> httpx.Response:
    body = {
        "model": "some/model:free",
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 20},
    }
    return httpx.Response(200, json=body)


def test_openrouter_requests_schema_constrained_output_and_parses_it():
    provider, sent = openrouter(completion("```json\n" + json.dumps(PLAN) + "\n```"))

    result = provider.generate(request())

    assert result.output.tasks[0].id == "T1" and result.usage == {"input_tokens": 50, "output_tokens": 20}
    body = sent[0]
    assert body["response_format"]["type"] == "json_schema"
    assert body["provider"] == {"require_parameters": True}
    schema = body["response_format"]["json_schema"]["schema"]
    task_schema = schema["$defs"]["Task"]
    assert set(task_schema["required"]) == set(task_schema["properties"])  # strict: everything required
    assert "default" not in json.dumps(schema)


def test_openrouter_strict_schema_keeps_fields_named_like_keywords():
    schema = strict_schema(RequirementSpec.model_json_schema())

    assert "title" in schema["properties"] and "title" in schema["required"]  # a field, not the keyword
    assert set(schema["required"]) == set(RequirementSpec.model_fields)
    assert '"title": "' not in json.dumps(schema)  # the keyword itself is gone


def test_openrouter_waits_out_rate_limits_then_succeeds():
    waits: list[float] = []
    limited = httpx.Response(429, headers={"retry-after": "7"}, text="rate limited")
    provider, sent = openrouter(limited, limited, completion(json.dumps(PLAN)), waits=waits)

    assert provider.generate(request()).output.summary == "s"
    assert waits == [7.0, 7.0] and len(sent) == 3


@pytest.mark.parametrize(
    "reply, expected",
    [
        (httpx.Response(429, text="Rate limit exceeded: free-models-per-day"), PermanentError),
        (httpx.Response(503, text="overloaded"), TransientError),
        (httpx.Response(401, text="bad key"), PermanentError),
        (httpx.Response(200, json={"error": {"code": 502, "message": "upstream died"}}), TransientError),
        (completion("Sure! Here is a plan without JSON."), AgentOutputError),
        (completion('{"summary": "missing fields"}'), AgentOutputError),
        (completion(json.dumps(PLAN), finish_reason="length"), AgentOutputError),
    ],
)
def test_openrouter_maps_failures_onto_the_retry_taxonomy(reply, expected):
    provider, _ = openrouter(reply)
    with pytest.raises(expected):
        provider.generate(request())


def test_openrouter_needs_a_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(PermanentError, match="OPENROUTER_API_KEY"):
        OpenRouterProvider()


@pytest.mark.parametrize(
    "stop_reason, expected",
    [("refusal", PermanentError), ("max_tokens", AgentOutputError), ("end_turn", AgentOutputError)],
)
def test_live_provider_never_returns_unusable_output(stop_reason, expected):
    provider, _ = provider_returning(response(stop_reason, parsed=None))
    with pytest.raises(expected):
        provider.generate(request())
