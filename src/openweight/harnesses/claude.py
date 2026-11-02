"""Claude Code driver: ``claude --print`` with a stream-json event log.

Tracing comes from the ``@mlflow/claude-code`` plugin baked into the sandbox image: its Stop hook
writes the session's trace to the experiment in ``MLFLOW_EXPERIMENT_ID``, with the session id in the
trace's ``mlflow.trace.session`` metadata. The runner chooses that id (``--session-id``) and uses
it to find the trace afterwards.
"""

import json
from pathlib import Path
from typing import Any

from openweight.branches import Evaluation
from openweight.harnesses.base import FAILED, OK, RATE_LIMITED, TIMEOUT, StepResult
from openweight.sandbox import HARNESS_CONFIG_MOUNT, TIMEOUT_EXIT, Sandbox

# Where the sandbox image's `npm install --global` puts the tracing plugin.
MLFLOW_PLUGIN_DIR = "/usr/local/lib/node_modules/@mlflow/claude-code"
RATE_LIMIT_REJECTED = "rejected"
RATE_LIMIT_HTTP_STATUS = 429


class ClaudeHarness:
    name = "claude"
    credentials = ("CLAUDE_CODE_OAUTH_TOKEN",)
    traces_in_sandbox = True
    prompt_on_stdin = False

    def check(self, evaluation: Evaluation) -> None:
        """Claude Code checks the model itself; a subscription has no model list to ask."""

    def budget(self, evaluation: Evaluation) -> float | None:
        """None: a subscription has usage limits, which Claude Code reports when it hits one."""
        return None

    def command(self, prompt: str, evaluation: Evaluation, session_id: str) -> list[str]:
        effort = ["--effort", evaluation.effort] if evaluation.effort else []
        return [
            "claude", "--print", prompt,
            "--model", evaluation.model,
            *effort,
            "--output-format", "stream-json", "--verbose",
            "--session-id", session_id,
            "--settings", f"{HARNESS_CONFIG_MOUNT}/claude.settings.json",
            # --strict-mcp-config keeps every MCP server but the configured ones out of the run.
            "--mcp-config", f"{HARNESS_CONFIG_MOUNT}/mcp.json", "--strict-mcp-config",
            "--plugin-dir", f"{HARNESS_CONFIG_MOUNT}/plugins",
            "--plugin-dir", MLFLOW_PLUGIN_DIR,
            "--dangerously-skip-permissions",
        ]  # fmt: skip

    def finish(self, box: Sandbox, events: Path) -> None:
        """Nothing to do: the event stream has the totals and the plugin writes the trace."""

    def parse(self, events: Path, exit_code: int) -> StepResult:
        session_id = None
        result: dict[str, Any] | None = None
        rate_limit: dict[str, Any] = {}
        tool_uses: set[str] = set()
        for event in _events(events):
            session_id = event.get("session_id") or session_id
            kind = event.get("type")
            if kind == "assistant":
                # A message with several content blocks arrives as several events, so tool calls
                # are counted by id.
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "tool_use":
                        tool_uses.add(block.get("id", f"#{len(tool_uses)}"))
            elif kind == "rate_limit_event":
                rate_limit = event.get("rate_limit_info") or {}
            elif kind == "result":
                result = event

        succeeded = (
            result is not None
            and not result.get("is_error")
            and result.get("subtype") == "success"
            and exit_code == 0
        )
        limited = not succeeded and (
            rate_limit.get("status") == RATE_LIMIT_REJECTED
            or (result or {}).get("api_error_status") == RATE_LIMIT_HTTP_STATUS
        )
        if exit_code == TIMEOUT_EXIT:
            status, error = TIMEOUT, "timed out"
        elif succeeded:
            status, error = OK, None
        elif limited:
            status, error = RATE_LIMITED, _error(result, exit_code)
        else:
            status, error = FAILED, _error(result, exit_code)

        usage = _usage(result or {})
        return StepResult(
            status=status,
            exit_code=exit_code,
            session_id=session_id,
            error=error,
            cost_usd=(result or {}).get("total_cost_usd"),
            num_turns=(result or {}).get("num_turns"),
            tool_calls=len(tool_uses),
            rate_limit_resets_at=rate_limit.get("resetsAt") if limited else None,
            **usage,
        )


def _events(path: Path) -> list[dict[str, Any]]:
    """The JSON objects in a stream-json log, skipping anything else Claude Code printed."""
    events = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _usage(result: dict[str, Any]) -> dict[str, int | None]:
    """Token counts summed over every model the session used (subagents may use others)."""
    by_model = result.get("modelUsage")
    if by_model:
        keys = {
            "input_tokens": "inputTokens",
            "output_tokens": "outputTokens",
            "cache_read_tokens": "cacheReadInputTokens",
            "cache_write_tokens": "cacheCreationInputTokens",
        }
        return {
            name: sum(int(usage.get(key, 0)) for usage in by_model.values())
            for name, key in keys.items()
        }
    usage = result.get("usage") or {}
    return {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_read_tokens": usage.get("cache_read_input_tokens"),
        "cache_write_tokens": usage.get("cache_creation_input_tokens"),
    }


def _error(result: dict[str, Any] | None, exit_code: int) -> str:
    if result is None:
        return f"claude exited {exit_code} without a result"
    message = result.get("result") or result.get("subtype") or "error"
    return str(message)[:500]
