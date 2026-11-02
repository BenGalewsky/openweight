"""Claude Code driver: ``claude --print`` with a stream-json event log.

Tracing comes from the ``@mlflow/claude-code`` plugin baked into the sandbox image: its Stop hook
writes the session's trace to the experiment in ``MLFLOW_EXPERIMENT_ID``, with the session id in the
trace's ``mlflow.trace.session`` metadata. The runner chooses that id (``--session-id``) and uses
it to find the trace afterwards.

Models named ``openrouter/<model>`` (e.g. ``openrouter/qwen/qwen3.8-max-0902``) run on OpenRouter's
Anthropic-compatible API with the key in ``OPEN_ROUTER_KEY`` instead of the subscription. Every
model tier Claude Code would pick on its own (subagents, background tasks) is mapped to the model
under test, so no session calls reach a Claude model. Claude Code prices models it does not know
at Claude's rates, so the cost of OpenRouter models (ids with a slash in ``modelUsage``) is taken
from OpenRouter's list prices instead.
"""

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import requests

from openweight.branches import Evaluation
from openweight.harnesses.base import FAILED, OK, RATE_LIMITED, TIMEOUT, HarnessError, StepResult
from openweight.harnesses.opencode import served_models
from openweight.sandbox import HARNESS_CONFIG_MOUNT, TIMEOUT_EXIT, Sandbox

# Where the sandbox image's `npm install --global` puts the tracing plugin.
MLFLOW_PLUGIN_DIR = "/usr/local/lib/node_modules/@mlflow/claude-code"
RATE_LIMIT_REJECTED = "rejected"
RATE_LIMIT_HTTP_STATUS = 429
OPENROUTER = "openrouter"
OPENROUTER_BASE_URL = "https://openrouter.ai/api"
OPENROUTER_KEY = "OPEN_ROUTER_KEY"
# The variables naming the models Claude Code picks for each tier and for its subagents.
MODEL_TIERS = (
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
)
PRICES_TIMEOUT_S = 30
# The token counts in `modelUsage` and the OpenRouter price (USD per token) each is billed at.
PRICED_TOKENS = {
    "inputTokens": "prompt",
    "outputTokens": "completion",
    "cacheReadInputTokens": "input_cache_read",
    "cacheCreationInputTokens": "input_cache_write",
}

logger = logging.getLogger(__name__)

# OpenRouter's `pricing` of each model it lists, by model id.
Prices = dict[str, dict[str, Any]]


class ClaudeHarness:
    name = "claude"
    traces_in_sandbox = True
    prompt_on_stdin = False

    def __init__(self, prices: Callable[[], Prices] | None = None) -> None:
        self.prices = prices or openrouter_prices

    def credentials(self, evaluation: Evaluation) -> tuple[str, ...]:
        if openrouter_model(evaluation):
            return (OPENROUTER_KEY,)
        return ("CLAUDE_CODE_OAUTH_TOKEN",)

    def check(self, evaluation: Evaluation) -> None:
        """Fail unless OpenRouter lists an ``openrouter/...`` model. Claude Code checks the others
        itself; a subscription has no model list to ask."""
        model = openrouter_model(evaluation)
        if model and model not in served_models(f"{OPENROUTER_BASE_URL}/v1", ""):
            raise HarnessError(f"OpenRouter does not serve {model}")

    def budget(self, evaluation: Evaluation) -> float | None:
        """None: a subscription has usage limits, which Claude Code reports when it hits one, and
        OpenRouter reports no budget."""
        return None

    def command(self, prompt: str, evaluation: Evaluation, session_id: str) -> list[str]:
        effort = ["--effort", evaluation.effort] if evaluation.effort else []
        model = openrouter_model(evaluation)
        claude = [
            "claude", "--print", prompt,
            "--model", model or evaluation.session_model,
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
        if not model:
            return claude
        return [
            "env", f"ANTHROPIC_BASE_URL={OPENROUTER_BASE_URL}", "ANTHROPIC_API_KEY=",
            *(f"{tier}={model}" for tier in MODEL_TIERS),
            # The shell copies the key, so only its name is in the arguments.
            "sh", "-c", f'ANTHROPIC_AUTH_TOKEN="${OPENROUTER_KEY}" exec "$@"', "sh", *claude,
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
            cost_usd=self._cost(result or {}),
            num_turns=(result or {}).get("num_turns"),
            tool_calls=len(tool_uses),
            rate_limit_resets_at=rate_limit.get("resetsAt") if limited else None,
            **usage,
        )

    def _cost(self, result: dict[str, Any]) -> float | None:
        """Claude Code's estimate, with OpenRouter models priced at OpenRouter's list prices; None
        if one of them cannot be priced."""
        by_model = result.get("modelUsage") or {}
        if not any("/" in model for model in by_model):
            return result.get("total_cost_usd")
        try:
            listed = self.prices()
        except HarnessError as e:
            logger.warning("cannot price the OpenRouter models: %s", e)
            return None
        cost = 0.0
        for model, usage in by_model.items():
            if "/" not in model:
                cost += float(usage.get("costUSD", 0))
            elif model in listed:
                pricing = listed[model]
                cost += sum(
                    int(usage.get(count, 0)) * float(pricing.get(price) or 0)
                    for count, price in PRICED_TOKENS.items()
                )
            else:
                logger.warning("OpenRouter does not list %s, so the session has no cost", model)
                return None
        return cost


def openrouter_prices() -> Prices:
    """The ``pricing`` OpenRouter lists for each model (USD per token, as strings)."""
    url = f"{OPENROUTER_BASE_URL}/v1/models"
    try:
        response = requests.get(url, timeout=PRICES_TIMEOUT_S)
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as e:
        raise HarnessError(f"cannot list the models at {url}: {e}") from None
    return {model["id"]: model.get("pricing") or {} for model in data.get("data", [])}


def openrouter_model(evaluation: Evaluation) -> str | None:
    """OpenRouter's id of an ``openrouter/<model>`` session model, or None for any other."""
    provider, _, model = evaluation.session_model.partition("/")
    return (model or None) if provider == OPENROUTER else None


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
