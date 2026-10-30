import json
from pathlib import Path
from typing import Any

from openweight.branches import Evaluation
from openweight.harnesses import base
from openweight.harnesses.claude import MLFLOW_PLUGIN_DIR, ClaudeHarness
from openweight.sandbox import TIMEOUT_EXIT

SESSION = "827bfe98-0230-49e6-9137-50a49580e254"


def assistant(*blocks: dict[str, Any]) -> dict[str, Any]:
    return {"type": "assistant", "message": {"content": list(blocks)}, "session_id": SESSION}


def tool_use(id: str) -> dict[str, Any]:
    return {"type": "tool_use", "id": id, "name": "Bash", "input": {}}


def rate_limit(status: str, resets_at: int = 1793358600) -> dict[str, Any]:
    info = {"status": status, "resetsAt": resets_at, "rateLimitType": "five_hour"}
    return {"type": "rate_limit_event", "rate_limit_info": info, "session_id": SESSION}


def result(**fields: Any) -> dict[str, Any]:
    event = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 4,
        "result": "done",
        "session_id": SESSION,
        "total_cost_usd": 0.25,
        "usage": {"input_tokens": 4, "output_tokens": 114},
        "modelUsage": {
            "claude-sonnet-5-5": {
                "inputTokens": 10, "outputTokens": 200,
                "cacheReadInputTokens": 3000, "cacheCreationInputTokens": 500,
            },
            "claude-haiku-5-5": {
                "inputTokens": 1, "outputTokens": 20,
                "cacheReadInputTokens": 0, "cacheCreationInputTokens": 50,
            },
        },
    }  # fmt: skip
    return event | fields


def write_events(path: Path, *events: dict[str, Any]) -> Path:
    lines = ["npm warn something that is not JSON"] + [json.dumps(e) for e in events]
    path.write_text("\n".join(lines) + "\n")
    return path


def test_command() -> None:
    command = ClaudeHarness().command(
        "Do step 1.", Evaluation("claude", "claude-sonnet-5-5", "high"), SESSION
    )
    assert command[:3] == ["claude", "--print", "Do step 1."]
    pairs = dict(zip(command, command[1:], strict=False))
    assert pairs["--model"] == "claude-sonnet-5-5"
    assert pairs["--effort"] == "high"
    assert pairs["--output-format"] == "stream-json"
    assert pairs["--session-id"] == SESSION
    assert pairs["--settings"] == "/harness_config/claude.settings.json"
    assert pairs["--mcp-config"] == "/harness_config/mcp.json"
    assert [command[i + 1] for i, a in enumerate(command) if a == "--plugin-dir"] == [
        "/harness_config/plugins",
        MLFLOW_PLUGIN_DIR,
    ]
    assert {"--verbose", "--strict-mcp-config", "--dangerously-skip-permissions"} <= set(command)


def test_command_without_effort() -> None:
    command = ClaudeHarness().command("x", Evaluation("claude", "claude-opus-5-5"), SESSION)
    assert "--effort" not in command


def test_settings_file_is_valid() -> None:
    settings = Path(__file__).parents[1] / "harness_config" / "claude.settings.json"
    data = json.loads(settings.read_text())
    assert data["env"]["MLFLOW_CLAUDE_TRACING_ENABLED"] == "true"
    # The object form: older versions reject a boolean and skip the whole file.
    assert data["attribution"] == {"commit": "", "pr": "", "sessionUrl": False}


def test_parse_success(tmp_path: Path) -> None:
    events = write_events(
        tmp_path / "e.jsonl",
        {"type": "system", "subtype": "init", "session_id": SESSION},
        rate_limit("allowed"),
        assistant({"type": "text", "text": "hi"}, tool_use("t1")),
        assistant(tool_use("t1")),  # the same block repeated in a split message
        assistant(tool_use("t2")),
        result(),
    )
    r = ClaudeHarness().parse(events, 0)
    assert r.status == base.OK
    assert r.session_id == SESSION
    assert r.error is None
    assert r.tool_calls == 2
    assert r.num_turns == 4
    assert r.cost_usd == 0.25
    # Summed over both models, not the top-level `usage` of the last turn.
    assert (r.input_tokens, r.output_tokens) == (11, 220)
    assert (r.cache_read_tokens, r.cache_write_tokens) == (3000, 550)
    assert r.rate_limit_resets_at is None
    assert r.metrics()["tool_calls"] == 2.0


def test_parse_error_result(tmp_path: Path) -> None:
    events = write_events(
        tmp_path / "e.jsonl", result(subtype="error_during_execution", is_error=True, result=None)
    )
    r = ClaudeHarness().parse(events, 1)
    assert r.status == base.FAILED
    assert r.error == "error_during_execution"


def test_parse_no_result(tmp_path: Path) -> None:
    events = write_events(tmp_path / "e.jsonl", assistant(tool_use("t1")))
    r = ClaudeHarness().parse(events, 1)
    assert r.status == base.FAILED
    assert r.error == "claude exited 1 without a result"
    assert r.cost_usd is None and r.input_tokens is None
    assert r.metrics() == {"tool_calls": 1.0}


def test_parse_timeout(tmp_path: Path) -> None:
    events = write_events(tmp_path / "e.jsonl", assistant(tool_use("t1")))
    r = ClaudeHarness().parse(events, TIMEOUT_EXIT)
    assert r.status == base.TIMEOUT


def test_parse_rate_limited(tmp_path: Path) -> None:
    events = write_events(
        tmp_path / "e.jsonl",
        rate_limit("rejected", 1793400000),
        result(is_error=True, subtype="success", result="You've hit your limit"),
    )
    r = ClaudeHarness().parse(events, 1)
    assert r.status == base.RATE_LIMITED
    assert r.rate_limit_resets_at == 1793400000
    assert r.error == "You've hit your limit"


def test_parse_rate_limited_by_http_status(tmp_path: Path) -> None:
    events = write_events(tmp_path / "e.jsonl", result(is_error=True, api_error_status=429))
    assert ClaudeHarness().parse(events, 1).status == base.RATE_LIMITED


def test_rejected_limit_after_success_is_ok(tmp_path: Path) -> None:
    events = write_events(tmp_path / "e.jsonl", rate_limit("rejected"), result())
    assert ClaudeHarness().parse(events, 0).status == base.OK
