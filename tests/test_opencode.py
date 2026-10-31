import json
import os
import subprocess
import tomllib
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import requests
from mlflow import MlflowClient
from mlflow.entities import SpanType

from openweight.branches import Evaluation
from openweight.harnesses import base, opencode
from openweight.harnesses.opencode import EXPORT_EVENT, OpenCodeHarness
from openweight.sandbox import TIMEOUT_EXIT
from openweight.tracking import SESSION_METADATA

ROOT = Path(__file__).parents[1]
SESSION = "ses_e72dbb3e9ffewnN8WE8l436AlI"
CHILD = "ses_e72da44c3ffeKkjHjRfONeywa3"
MODEL = {"id": "qwen3.8-flash", "providerID": "lumen", "variant": "default"}


def event(kind: str, **fields: Any) -> dict[str, Any]:
    return {"type": kind, "timestamp": 1793369330968, "sessionID": SESSION} | fields


def step_finish(input: int, output: int, cache_read: int = 0, cost: float = 0.001) -> dict:
    cache = {"read": cache_read, "write": 0}
    tokens = {"input": input, "output": output, "reasoning": 0, "cache": cache}
    return event("step_finish", part={"type": "step-finish", "cost": cost, "tokens": tokens})


def tool_use(name: str) -> dict[str, Any]:
    return event("tool_use", part={"type": "tool", "tool": name, "state": {"status": "completed"}})


# A session as the stream shows it: the last model call has no step_finish.
STREAM = [
    event("step_start", part={"type": "step-start"}),
    tool_use("write"),
    step_finish(5530, 61),
    event("step_start", part={"type": "step-start"}),
    tool_use("subagent"),
    step_finish(899, 86, cache_read=5568),
    event("step_start", part={"type": "step-start"}),
    event("text", part={"type": "text", "text": "Done."}),
]


def tool(name: str, child: str | None = None, status: str = "completed") -> dict[str, Any]:
    metadata = {"sessionID": child} if child else {"truncated": False}
    return {
        "type": "tool",
        "id": f"call_{name}",
        "name": name,
        "state": {
            "status": status,
            "input": {"path": "/workspace/hello.txt"},
            "content": [{"type": "text", "text": f"{name} output"}],
            "metadata": metadata,
        },
        "time": {"created": 1793369422021, "completed": 1793369429804},
    }


def assistant(*content: dict[str, Any], error: dict | None = None, **tokens: int) -> dict:
    usage = {"input": 100, "output": 10, "reasoning": 0, "cache": {"read": 0, "write": 0}} | tokens
    return {
        "type": "assistant",
        "time": {"created": 1793369329008, "completed": 1793369331358},
        "model": MODEL,
        "content": list(content),
        "finish": "error" if error else "stop",
        "error": error,
        "cost": 0.0001,
        "tokens": usage,
    }


def export(
    session: str, *messages: dict[str, Any], outcome: str | None = "succeeded", **info: Any
) -> dict[str, Any]:
    tokens = {"input": 7313, "output": 563, "reasoning": 7, "cache": {"read": 12096, "write": 3}}
    return {
        "info": {
            "id": session,
            "model": MODEL,
            "cost": 0.0013,
            "tokens": tokens,
            "outcome": outcome,
            "time": {"created": 1793369328690, "idle": 1793369335332},
        }
        | info,
        "messages": [
            {"type": "user", "text": "Create hello.txt", "time": {"created": 1793369328966}},
            *messages,
            {"type": "idle", "outcome": outcome, "time": {"created": 1793369335332}},
        ],
    }


ROOT_EXPORT = export(
    SESSION,
    assistant({"type": "reasoning", "text": "Simple."}, tool("write")),
    assistant(tool("subagent", child=CHILD)),
    assistant({"type": "text", "text": "Done."}),
)
CHILD_EXPORT = export(
    CHILD,
    assistant(tool("glob"), tool("read")),
    assistant({"type": "text", "text": "3 files."}),
    cost=0.0023,
    parentID=SESSION,
    tokens={"input": 13974, "output": 708, "reasoning": 0, "cache": {"read": 2112, "write": 0}},
)


def write_events(path: Path, *events: dict[str, Any]) -> Path:
    lines = ["opencode printed something that is not JSON"] + [json.dumps(e) for e in events]
    path.write_text("\n".join(lines) + "\n")
    return path


def exported(*exports: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"type": EXPORT_EVENT, "export": e} for e in exports]


def test_command_takes_the_prompt_on_stdin() -> None:
    harness = OpenCodeHarness()
    command = harness.command("Do step 1.", Evaluation("opencode", "lumen/glm-5.3"), "unused")
    assert harness.prompt_on_stdin and "Do step 1." not in command
    assert command[:2] == ["env", "OPENCODE_CONFIG=/harness_config/opencode.json"]
    assert command[2:5] == ["opencode", "run", "--standalone"]
    pairs = dict(zip(command, command[1:], strict=False))
    assert pairs["--model"] == "lumen/glm-5.3"
    assert pairs["--format"] == "json"
    assert "--auto" in command


def test_command_effort_is_the_model_variant() -> None:
    command = OpenCodeHarness().command("x", Evaluation("opencode", "lumen/glm-5.3", "high"), "")
    assert command[command.index("--model") + 1] == "lumen/glm-5.3#high"


def test_parse_totals_from_exports(tmp_path: Path) -> None:
    events = write_events(tmp_path / "e.jsonl", *STREAM, *exported(ROOT_EXPORT, CHILD_EXPORT))
    r = OpenCodeHarness().parse(events, 0)
    assert r.status == base.OK
    assert r.session_id == SESSION
    assert r.error is None
    # Both sessions' totals: the stream lacks the last call and the subagent's session.
    assert r.input_tokens == 7313 + 13974
    assert r.output_tokens == 563 + 7 + 708  # reasoning counts as output
    assert (r.cache_read_tokens, r.cache_write_tokens) == (12096 + 2112, 3)
    assert r.cost_usd == pytest.approx(0.0013 + 0.0023)
    assert r.num_turns == 3  # the top-level session's model calls
    assert r.tool_calls == 4  # write, subagent, and the subagent's glob and read
    assert r.rate_limit_resets_at is None


def test_parse_outcome_beats_exit_code(tmp_path: Path) -> None:
    # OpenCode has exited 1 after a session it recorded as succeeded, with no error anywhere.
    events = write_events(tmp_path / "e.jsonl", *STREAM, *exported(ROOT_EXPORT))
    r = OpenCodeHarness().parse(events, 1)
    assert r.status == base.OK
    assert r.exit_code == 1
    unfinished = export(SESSION, assistant(), outcome=None)
    events = write_events(tmp_path / "e.jsonl", *STREAM, *exported(unfinished))
    assert OpenCodeHarness().parse(events, 0).status == base.FAILED


def test_parse_falls_back_to_the_stream(tmp_path: Path) -> None:
    r = OpenCodeHarness().parse(write_events(tmp_path / "e.jsonl", *STREAM), 0)
    assert r.status == base.OK  # without an export, the exit code decides
    assert (
        OpenCodeHarness().parse(write_events(tmp_path / "f.jsonl", *STREAM), 1).status == "failed"
    )
    assert (r.input_tokens, r.output_tokens, r.cache_read_tokens) == (6429, 147, 5568)
    assert r.cost_usd == pytest.approx(0.002)
    assert r.num_turns == 3
    assert r.tool_calls == 2


def test_parse_error_event(tmp_path: Path) -> None:
    error = {"type": "provider.auth", "message": "Invalid or revoked API key", "status": 401}
    events = write_events(
        tmp_path / "e.jsonl",
        event("step_start", part={"type": "step-start"}),
        event("error", error=error),
        *exported(export(SESSION, assistant(error=error), outcome="failed", cost=0)),
    )
    r = OpenCodeHarness().parse(events, 1)
    assert r.status == base.FAILED
    assert r.error == "Invalid or revoked API key"
    assert r.session_id == SESSION


def test_parse_failed_outcome_without_error_event(tmp_path: Path) -> None:
    error = {"type": "provider.no-route", "message": "Model unavailable"}
    failed = export(SESSION, assistant(), assistant(error=error), outcome="failed")
    r = OpenCodeHarness().parse(write_events(tmp_path / "e.jsonl", *exported(failed)), 0)
    assert r.status == base.FAILED
    assert r.error == "Model unavailable"


def test_parse_recovered_error_is_ok(tmp_path: Path) -> None:
    # An error on an earlier model call that the session got past does not fail it.
    recovered = export(SESSION, assistant(error={"message": "overloaded"}), assistant())
    r = OpenCodeHarness().parse(write_events(tmp_path / "e.jsonl", *exported(recovered)), 0)
    assert r.status == base.OK


def test_parse_rate_limited(tmp_path: Path) -> None:
    error = {"type": "provider.api", "message": "Too many requests", "status": 429}
    events = write_events(tmp_path / "e.jsonl", event("error", error=error))
    r = OpenCodeHarness().parse(events, 1)
    assert r.status == base.RATE_LIMITED
    assert r.error == "Too many requests"
    assert r.rate_limit_resets_at is None


def test_parse_timeout_keeps_usage(tmp_path: Path) -> None:
    aborted = {"type": "aborted", "message": "Step interrupted"}
    interrupted = export(SESSION, assistant(tool("shell")), assistant(error=aborted), outcome=None)
    events = write_events(
        tmp_path / "e.jsonl",
        *STREAM[:3],
        event("error", error={"type": "unknown", "message": "socket closed"}),
        *exported(interrupted),
    )
    r = OpenCodeHarness().parse(events, TIMEOUT_EXIT)
    assert r.status == base.TIMEOUT
    assert r.error == "timed out"
    assert r.input_tokens == 7313  # what the session used before it was stopped


def test_parse_nothing(tmp_path: Path) -> None:
    r = OpenCodeHarness().parse(write_events(tmp_path / "e.jsonl"), 1)
    assert r.status == base.FAILED
    assert r.error == "opencode exited 1"
    assert r.session_id is None
    assert r.cost_usd is None and r.input_tokens is None and r.num_turns is None
    assert r.metrics() == {"tool_calls": 0.0}


class FakeBox:
    """Answers ``opencode session export`` from ``exports``."""

    def __init__(self, exports: Sequence[dict[str, Any]]) -> None:
        self.exports = {e["info"]["id"]: e for e in exports}
        self.commands: list[list[str]] = []

    def exec(self, command: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(command))
        found = self.exports.get(command[-1])
        if found is None:
            return subprocess.CompletedProcess(command, 1, "", f"Session not found: {command[-1]}")
        return subprocess.CompletedProcess(command, 0, json.dumps(found, indent=2), "")


@pytest.fixture
def client(tracking_uri: str, monkeypatch: pytest.MonkeyPatch) -> MlflowClient:
    """Fluent mlflow logging traces to a fresh experiment in the local store."""
    client = MlflowClient(tracking_uri)
    experiment_id = client.create_experiment(f"test-{uuid.uuid4().hex[:8]}")
    monkeypatch.setenv("MLFLOW_TRACKING_URI", tracking_uri)
    monkeypatch.setenv("MLFLOW_EXPERIMENT_ID", experiment_id)
    monkeypatch.setenv("MLFLOW_ENABLE_ASYNC_TRACE_LOGGING", "false")
    return client


def test_finish_appends_exports_and_writes_the_trace(tmp_path: Path, client: MlflowClient) -> None:
    events = tmp_path / "e.jsonl"
    events.write_text("\n".join(json.dumps(e) for e in STREAM))  # no final newline
    box = FakeBox([ROOT_EXPORT, CHILD_EXPORT])
    OpenCodeHarness().finish(box, events)  # type: ignore[arg-type]

    assert [c[-1] for c in box.commands] == [SESSION, CHILD]
    assert box.commands[0][:4] == ["opencode", "session", "export", "--standalone"]
    lines = [json.loads(line) for line in events.read_text().splitlines()]
    assert lines[: len(STREAM)] == STREAM
    assert lines[len(STREAM) :] == exported(ROOT_EXPORT, CHILD_EXPORT)

    [trace] = client.search_traces(
        locations=[os.environ["MLFLOW_EXPERIMENT_ID"]],
        filter_string=f"metadata.`{SESSION_METADATA}` = '{SESSION}'",
    )
    spans = trace.data.spans
    root = next(s for s in spans if s.parent_id is None)
    assert root.name == "opencode_conversation"
    assert root.inputs == {"prompt": "Create hello.txt"}
    assert root.outputs == {"response": "Done."}
    by_name: dict[str, list[Any]] = {}
    for span in spans:
        by_name.setdefault(span.name, []).append(span)
    assert len(by_name["llm_call"]) == 5  # three in the session, two in the subagent's
    assert {s.span_type for s in by_name["llm_call"]} == {SpanType.LLM}
    [subagent] = by_name["tool_subagent"]
    assert subagent.parent_id == root.span_id
    # The subagent's session hangs under the tool call that ran it.
    assert {s.name for s in spans if s.parent_id == subagent.span_id} == {
        "llm_call",
        "tool_glob",
        "tool_read",
    }
    usage = by_name["llm_call"][0].get_attribute("mlflow.chat.tokenUsage")
    assert usage["input_tokens"] == 100 and usage["total_tokens"] == 110


def test_finish_without_an_export(tmp_path: Path) -> None:
    events = write_events(tmp_path / "e.jsonl", *STREAM)
    before = events.read_text()
    OpenCodeHarness().finish(FakeBox([]), events)  # type: ignore[arg-type]
    assert events.read_text() == before


def test_finish_without_a_session(tmp_path: Path) -> None:
    events = write_events(tmp_path / "e.jsonl")
    box = FakeBox([ROOT_EXPORT])
    OpenCodeHarness().finish(box, events)  # type: ignore[arg-type]
    assert box.commands == []


@pytest.fixture
def config(tmp_path: Path) -> Path:
    path = tmp_path / "opencode.json"
    options = {"baseURL": "https://lumen.example/v1", "apiKey": "{env:LUMEN_API_KEY}"}
    path.write_text(json.dumps({"provider": {"lumen": {"options": options}}}))
    return path


def test_check_lists_the_providers_models(config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def get(url: str, headers: dict[str, str], timeout: float) -> requests.Response:
        calls.append((url, headers))
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({"data": [{"id": "glm-5.3"}, {"id": "glm"}]}).encode()
        return response

    monkeypatch.setenv("LUMEN_API_KEY", "k3y")
    monkeypatch.setattr(opencode.requests, "get", get)
    harness = OpenCodeHarness(config)
    harness.check(Evaluation("opencode", "lumen/glm-5.3"))
    assert calls == [("https://lumen.example/v1/models", {"Authorization": "Bearer k3y"})]
    with pytest.raises(base.HarnessError, match="lumen does not serve glm-9 .it serves glm, glm"):
        harness.check(Evaluation("opencode", "lumen/glm-9"))
    with pytest.raises(base.HarnessError, match="provider 'other' is not configured"):
        harness.check(Evaluation("opencode", "other/glm-5.3"))
    with pytest.raises(base.HarnessError, match="provider/model ids"):
        harness.check(Evaluation("opencode", "glm-5.3"))


def test_check_unreachable_provider(config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def get(url: str, headers: dict[str, str], timeout: float) -> requests.Response:
        raise requests.ConnectionError("no route")

    monkeypatch.setattr(opencode.requests, "get", get)
    with pytest.raises(base.HarnessError, match="cannot list the models at"):
        OpenCodeHarness(config).check(Evaluation("opencode", "lumen/glm-5.3"))


def test_config_file() -> None:
    config = json.loads((ROOT / "harness_config" / "opencode.json").read_text())
    assert config["permissions"] == [{"action": "*", "resource": "*", "effect": "allow"}]
    # OpenCode 2 cannot load @mlflow/opencode; the driver writes the traces instead.
    assert "plugin" not in config and "plugins" not in config
    matrix = tomllib.loads((ROOT / "matrix.toml").read_text())
    for entry in matrix["eval"]:
        if entry["harness"] == "opencode":
            provider, _, model = entry["model"].partition("/")
            assert model in config["provider"][provider]["models"]
