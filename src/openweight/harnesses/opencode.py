"""OpenCode driver: ``opencode run --format json`` with the prompt on stdin.

OpenCode's JSON stream has an event per message part (``step_start``, ``text``, ``tool_use``,
``step_finish``, ``error``) but not the session's totals: the last model call never gets its
``step_finish``, and subagents run in child sessions whose events are not in the stream. After each
session the driver therefore exports it, and recursively every subagent session it started, with
``opencode session export``, and appends each export to the event log as an ``openweight.session``
event. `parse` takes the totals from those, or from the stream's ``step_finish`` events when there
are none.

``@mlflow/opencode`` 0.4.0 is an OpenCode 1 plugin that OpenCode 2 refuses to load, so the driver
writes each session's trace itself, on the host, from the exports. Like the plugin it puts the
session id in the trace's ``mlflow.trace.session`` metadata, where the runner looks for it.

The prompt goes in on stdin: OpenCode quotes a message given as an argument and escapes the double
quotes inside it.
"""

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import mlflow
import requests
from mlflow.entities import LiveSpan, SpanType
from mlflow.exceptions import MlflowException
from mlflow.tracing.constant import SpanAttributeKey, TokenUsageKey

from openweight.branches import Evaluation
from openweight.harnesses.base import (
    FAILED,
    OK,
    RATE_LIMITED,
    TIMEOUT,
    HarnessError,
    StepResult,
)
from openweight.sandbox import HARNESS_CONFIG_MOUNT, TIMEOUT_EXIT, Sandbox
from openweight.tracking import SESSION_METADATA

CONFIG = Path(__file__).resolve().parents[3] / "harness_config" / "opencode.json"
EXPORT_EVENT = "openweight.session"
EXPORT_TIMEOUT_S = 120
MODELS_TIMEOUT_S = 30
RATE_LIMIT_HTTP_STATUS = 429

logger = logging.getLogger(__name__)

Export = dict[str, Any]


class OpenCodeHarness:
    name = "opencode"
    # Every OpenCode model in the matrix is served by Lumen (see harness_config/opencode.json).
    credentials = ("LUMEN_API_KEY",)
    traces_in_sandbox = False
    prompt_on_stdin = True

    def __init__(self, config: Path = CONFIG) -> None:
        self.config = config

    def check(self, evaluation: Evaluation) -> None:
        """Fail unless the model's provider, as configured in ``opencode.json``, lists it."""
        base_url, api_key = self._provider(evaluation)
        provider, _, model = evaluation.model.partition("/")
        served = served_models(base_url, api_key)
        if model not in served:
            raise HarnessError(
                f"{provider} does not serve {model} (it serves {', '.join(sorted(served))})"
            )

    def budget(self, evaluation: Evaluation) -> float | None:
        """The ``coins_available`` the provider reports at ``<baseURL>/usage`` (Lumen does), or
        None if it reports none or cannot be reached."""
        return available_budget(*self._provider(evaluation))

    def _provider(self, evaluation: Evaluation) -> tuple[str, str]:
        """The base URL and API key of the model's provider in ``opencode.json``."""
        provider, _, model = evaluation.model.partition("/")
        if not model:
            raise HarnessError(f"OpenCode models are provider/model ids, not {evaluation.model!r}")
        providers = json.loads(self.config.read_text()).get("provider", {})
        if provider not in providers:
            raise HarnessError(f"provider {provider!r} is not configured in {self.config.name}")
        options = providers[provider].get("options", {})
        return options["baseURL"], _resolve_env(options.get("apiKey", ""))

    def command(self, prompt: str, evaluation: Evaluation, session_id: str) -> list[str]:
        model = evaluation.model + (f"#{evaluation.effort}" if evaluation.effort else "")
        return [
            "env", f"OPENCODE_CONFIG={HARNESS_CONFIG_MOUNT}/opencode.json",
            # --standalone runs a private server rather than leaving a background service behind.
            "opencode", "run", "--standalone",
            "--model", model,
            "--format", "json",
            "--auto",
        ]  # fmt: skip

    def finish(self, box: Sandbox, events: Path) -> None:
        session_id = _session_id(_events(events))
        if session_id is None:
            return
        exports = export_sessions(box, session_id)
        if not exports:
            return
        text = events.read_text(errors="replace")
        with events.open("a") as out:
            if text and not text.endswith("\n"):
                out.write("\n")
            for export in exports:
                out.write(json.dumps({"type": EXPORT_EVENT, "export": export}) + "\n")
        try:
            log_trace(exports)
        except MlflowException as e:
            logger.warning("could not write the trace of OpenCode session %s: %s", session_id, e)

    def parse(self, events: Path, exit_code: int) -> StepResult:
        stream = _events(events)
        exports = [event["export"] for event in stream if event.get("type") == EXPORT_EVENT]
        session_id = _session_id(stream)
        root = exports[0] if exports else None
        errors = [event.get("error") or {} for event in stream if event.get("type") == "error"]
        if root is not None and not errors:
            # A failed session's error is on its last message even when the stream lost it.
            last = (_assistant_messages(root) or [{}])[-1]
            errors = [last["error"]] if last.get("error") else []
        # The session's own outcome decides when there is one: OpenCode 2.0.23 has exited 1, with
        # nothing on stderr or in its log, after a session that went idle as "succeeded".
        if root is not None:
            succeeded = root["info"].get("outcome") == "succeeded"
        else:
            succeeded = exit_code == 0

        if exit_code == TIMEOUT_EXIT:
            status, error = TIMEOUT, "timed out"
        elif succeeded and not errors:
            status, error = OK, None
        elif any(_rate_limited(e) for e in errors):
            status, error = RATE_LIMITED, _error(errors, exit_code)
        else:
            status, error = FAILED, _error(errors, exit_code)

        if exports:
            tokens = [export["info"].get("tokens") or {} for export in exports]
            costs = [export["info"].get("cost") for export in exports]
            num_turns: int | None = len(_assistant_messages(exports[0]))
            tool_calls = sum(len(_tool_parts(export)) for export in exports)
        else:
            finished = [e.get("part") or {} for e in stream if e.get("type") == "step_finish"]
            tokens = [part.get("tokens") or {} for part in finished]
            costs = [part.get("cost") for part in finished]
            started = sum(1 for e in stream if e.get("type") == "step_start")
            num_turns = started or None
            tool_calls = sum(1 for e in stream if e.get("type") == "tool_use")
        known_costs = [cost for cost in costs if cost is not None]
        return StepResult(
            status=status,
            exit_code=exit_code,
            session_id=session_id,
            error=error,
            cost_usd=sum(known_costs) if known_costs else None,
            num_turns=num_turns,
            tool_calls=tool_calls,
            **_usage(tokens),
        )


def served_models(base_url: str, api_key: str) -> set[str]:
    """The model ids an OpenAI-compatible endpoint lists at ``<base_url>/models``."""
    url = base_url.rstrip("/") + "/models"
    try:
        response = requests.get(
            url, headers={"Authorization": f"Bearer {api_key}"}, timeout=MODELS_TIMEOUT_S
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as e:
        raise HarnessError(f"cannot list the models at {url}: {e}") from None
    return {model["id"] for model in data.get("data", [])}


def available_budget(base_url: str, api_key: str) -> float | None:
    """``coins_available`` from ``<base_url>/usage``, Lumen's spend budget, or None if the
    endpoint is missing, unreachable, or does not report it."""
    url = base_url.rstrip("/") + "/usage"
    try:
        response = requests.get(
            url, headers={"Authorization": f"Bearer {api_key}"}, timeout=MODELS_TIMEOUT_S
        )
        response.raise_for_status()
        coins = response.json().get("coins_available")
    except (requests.RequestException, ValueError, AttributeError) as e:
        logger.warning("cannot read the budget at %s: %s", url, e)
        return None
    return float(coins) if isinstance(coins, int | float) else None


def export_sessions(box: Sandbox, session_id: str) -> list[Export]:
    """``opencode session export`` of ``session_id`` and every subagent session under it, the
    top-level session first. Sessions that cannot be exported are left out."""
    exports: list[Export] = []
    pending, seen = [session_id], set()
    while pending:
        current = pending.pop(0)
        if current in seen:
            continue
        seen.add(current)
        proc = box.exec(
            ["opencode", "session", "export", "--standalone", current], timeout=EXPORT_TIMEOUT_S
        )
        try:
            export = json.loads(proc.stdout) if proc.returncode == 0 else None
        except json.JSONDecodeError:
            export = None
        if not isinstance(export, dict) or "info" not in export:
            logger.warning("could not export OpenCode session %s: %s", current, proc.stderr)
            continue
        exports.append(export)
        pending += [child for part in _tool_parts(export) if (child := _subagent_session(part))]
    return exports


def log_trace(exports: list[Export]) -> str:
    """Write the trace of the first session in ``exports``, with each subagent session's spans
    under the tool call that started it. Returns the trace id."""
    sessions = {export["info"]["id"]: export for export in exports}
    root = exports[0]
    info = root["info"]
    messages = root.get("messages") or []
    model = info.get("model") or {}
    prompt = next((m.get("text", "") for m in messages if m.get("type") == "user"), "")
    span = mlflow.start_span_no_context(
        "opencode_conversation",
        span_type=SpanType.AGENT,
        inputs={"prompt": prompt},
        attributes={"model": f"{model.get('providerID')}/{model.get('id')}"},
        metadata={SESSION_METADATA: info["id"]},
        start_time_ns=_ns(info.get("time", {}).get("created")),
    )
    _session_spans(span, root, sessions, {info["id"]})
    times = [t for m in messages for t in (m.get("time") or {}).values() if isinstance(t, int)]
    response = [_text(m.get("content"), "text") for m in _assistant_messages(root)]
    span.end(
        outputs={"response": next((r for r in reversed(response) if r.strip()), "")},
        attributes={"outcome": info.get("outcome") or "unfinished"},
        status="ERROR" if info.get("outcome") == "failed" else "OK",
        end_time_ns=_ns(max(times)) if times else None,
    )
    mlflow.flush_trace_async_logging()
    return span.trace_id


def _session_spans(
    parent: LiveSpan, export: Export, sessions: dict[str, Export], seen: set[str]
) -> None:
    """An LLM span per model call in ``export`` and a tool span per tool call, as children of
    ``parent``."""
    for message in _assistant_messages(export):
        time = message.get("time") or {}
        start = _ns(time.get("created"))
        model = message.get("model") or {}
        content = message.get("content") or []
        llm = mlflow.start_span_no_context(
            "llm_call",
            span_type=SpanType.LLM,
            parent_span=parent,
            inputs={"model": f"{model.get('providerID')}/{model.get('id')}"},
            attributes={
                SpanAttributeKey.CHAT_USAGE: _span_usage(message.get("tokens") or {}),
                "cost_usd": message.get("cost") or 0,
                "finish": message.get("finish") or "",
            },
            start_time_ns=start,
        )
        llm.end(
            outputs={
                "text": _text(content, "text"),
                "reasoning": _text(content, "reasoning"),
                "tool_calls": [part.get("name") for part in content if part.get("type") == "tool"],
            },
            status="ERROR" if message.get("error") else "OK",
            end_time_ns=_ns(time.get("completed")) or start,
        )
        for part in content:
            if part.get("type") == "tool":
                _tool_span(parent, part, sessions, seen)


def _tool_span(
    parent: LiveSpan, part: dict[str, Any], sessions: dict[str, Export], seen: set[str]
) -> None:
    state = part.get("state") or {}
    time = part.get("time") or {}
    start = _ns(time.get("created"))
    span = mlflow.start_span_no_context(
        f"tool_{part.get('name')}",
        span_type=SpanType.TOOL,
        parent_span=parent,
        inputs=state.get("input") or {},
        attributes={"tool_id": part.get("id") or "", "status": state.get("status") or ""},
        start_time_ns=start,
    )
    child = _subagent_session(part)
    if child in sessions and child not in seen:
        _session_spans(span, sessions[child], sessions, seen | {child})
    error = state.get("status") == "error"
    span.end(
        outputs={"error" if error else "result": _text(state.get("content"), "text")},
        status="ERROR" if error else "OK",
        end_time_ns=_ns(time.get("completed")) or start,
    )


def _events(path: Path) -> list[dict[str, Any]]:
    """The JSON objects in the event log, skipping anything else OpenCode printed."""
    events = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _session_id(events: list[dict[str, Any]]) -> str | None:
    return next((e["sessionID"] for e in events if e.get("sessionID")), None)


def _assistant_messages(export: Export) -> list[dict[str, Any]]:
    return [m for m in export.get("messages") or [] if m.get("type") == "assistant"]


def _tool_parts(export: Export) -> list[dict[str, Any]]:
    return [
        part
        for message in _assistant_messages(export)
        for part in message.get("content") or []
        if part.get("type") == "tool"
    ]


def _subagent_session(part: dict[str, Any]) -> str | None:
    """The session a tool call ran a subagent in, if it did."""
    metadata = (part.get("state") or {}).get("metadata") or {}
    session = metadata.get("sessionID")
    return session if isinstance(session, str) else None


def _text(content: list[dict[str, Any]] | None, kind: str) -> str:
    return "".join(part.get("text", "") for part in content or [] if part.get("type") == kind)


def _usage(tokens: list[dict[str, Any]]) -> dict[str, int | None]:
    """Token counts summed over model calls or sessions. OpenCode, like the providers, counts
    cached input apart from ``input``; it also counts reasoning apart from ``output``, which here
    includes it, as Claude's does."""
    if not tokens:
        return dict.fromkeys(
            ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
        )
    return {
        "input_tokens": sum(t.get("input", 0) for t in tokens),
        "output_tokens": sum(t.get("output", 0) + t.get("reasoning", 0) for t in tokens),
        "cache_read_tokens": sum((t.get("cache") or {}).get("read", 0) for t in tokens),
        "cache_write_tokens": sum((t.get("cache") or {}).get("write", 0) for t in tokens),
    }


def _span_usage(tokens: dict[str, Any]) -> dict[str, int]:
    usage = {
        TokenUsageKey.INPUT_TOKENS: tokens.get("input", 0),
        TokenUsageKey.OUTPUT_TOKENS: tokens.get("output", 0) + tokens.get("reasoning", 0),
        TokenUsageKey.CACHE_READ_INPUT_TOKENS: (tokens.get("cache") or {}).get("read", 0),
        TokenUsageKey.CACHE_CREATION_INPUT_TOKENS: (tokens.get("cache") or {}).get("write", 0),
    }
    return usage | {TokenUsageKey.TOTAL_TOKENS: sum(usage.values())}


def _ns(epoch_ms: int | None) -> int | None:
    return epoch_ms * 1_000_000 if epoch_ms is not None else None


def _rate_limited(error: dict[str, Any]) -> bool:
    kind = str(error.get("type", ""))
    return error.get("status") == RATE_LIMIT_HTTP_STATUS or bool(re.search(r"rate.?limit", kind))


def _error(errors: list[dict[str, Any]], exit_code: int) -> str:
    if not errors:
        return f"opencode exited {exit_code}"
    error = errors[-1]
    message = error.get("message") or error.get("type") or "error"
    return str(message)[:500]


def _resolve_env(value: str) -> str:
    """An ``opencode.json`` value with ``{env:NAME}`` references filled in."""
    return re.sub(r"\{env:(\w+)\}", lambda m: os.environ.get(m.group(1), ""), value)
