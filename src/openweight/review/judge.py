"""Judge drivers: a headless agent with read-only access that scores a phase against the rubric.

The judge runs on the host, in a read-only copy of the code (see `reviewer`), and answers with
JSON matching `rubric.SCHEMA`. Its session becomes an MLflow trace, written here, which the
reviewer links to the review run and gives the scores as assessments.

Codex (``codex exec``, on a ChatGPT subscription) is the judge. Its ``--json`` events report
tokens but neither the model nor the plan's quota, so those come from the session file Codex keeps
in ``~/.codex/sessions/``, which also has the times of each shell command for the trace.
"""

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import mlflow
from mlflow.entities import AssessmentSource, AssessmentSourceType, SpanType
from mlflow.tracing.constant import SpanAttributeKey, TokenUsageKey

from openweight.review import rubric
from openweight.tracking import SESSION_METADATA

OK = "ok"
FAILED = "failed"
TIMEOUT = "timeout"

CODEX_MODEL = "gpt-6.1-sol"
CODEX_EFFORT = "high"
# How much of a command's output a trace span keeps.
SPAN_OUTPUT_CHARS = 4000

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JudgeResult:
    """One judge session. Token counts follow `StepResult`: ``input_tokens`` excludes cached
    input, which is counted in ``cache_read_tokens``."""

    status: str
    exit_code: int
    duration_s: float
    started_ns: int
    session_id: str | None = None
    model: str | None = None
    error: str | None = None
    # The judge's final answer (the JSON review).
    answer: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    commands: int = 0
    # Quota used after the session, in percent of each window, e.g. {"5h": 3.0, "week": 0.0}.
    quota_used_pct: dict[str, float] = field(default_factory=dict)
    # Where the harness keeps the session (Codex's rollout file), if found.
    session_file: Path | None = None

    def metrics(self) -> dict[str, float]:
        values = {
            "judge_duration_s": self.duration_s,
            "judge_input_tokens": self.input_tokens,
            "judge_output_tokens": self.output_tokens,
            "judge_cache_read_tokens": self.cache_read_tokens,
            "judge_commands": self.commands,
        }
        values |= {f"judge_quota_{window}_pct": pct for window, pct in self.quota_used_pct.items()}
        return {key: float(value) for key, value in values.items() if value is not None}


class Judge(Protocol):
    name: str
    model: str

    def run(self, prompt: str, code: Path, log_dir: Path, timeout_s: float) -> JudgeResult:
        """Run one read-only session in ``code``, writing its logs to ``log_dir``."""
        ...

    def log_trace(self, result: JudgeResult, prompt: str) -> str:
        """Write the session's trace to the experiment and return its id."""
        ...


class CodexJudge:
    name = "codex"

    def __init__(
        self,
        model: str = CODEX_MODEL,
        effort: str = CODEX_EFFORT,
        sessions_dir: Path | None = None,
    ) -> None:
        self.model = model
        self.effort = effort
        codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        self.sessions_dir = sessions_dir or codex_home / "sessions"

    def command(self, code: Path, schema: Path, answer: Path) -> list[str]:
        """``codex exec`` reading the prompt from stdin. ``--ignore-user-config`` keeps the user's
        own MCP servers, profiles and model defaults out of the review (auth still comes from
        ``CODEX_HOME``)."""
        return [
            "codex", "exec",
            "--sandbox", "read-only",
            "--cd", str(code),
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--model", self.model,
            "--config", f"model_reasoning_effort={self.effort}",
            "--output-schema", str(schema),
            "--output-last-message", str(answer),
            "--json",
            "-",
        ]  # fmt: skip

    def run(self, prompt: str, code: Path, log_dir: Path, timeout_s: float) -> JudgeResult:
        log_dir.mkdir(parents=True, exist_ok=True)
        schema = log_dir / "schema.json"
        schema.write_text(json.dumps(rubric.SCHEMA, indent=2) + "\n")
        answer = log_dir / "answer.json"
        answer.unlink(missing_ok=True)
        events = log_dir / "events.jsonl"
        started_ns, started = time.time_ns(), time.monotonic()
        with events.open("w") as out:
            try:
                proc = subprocess.run(
                    self.command(code, schema, answer),
                    input=prompt, stdout=out, stderr=subprocess.PIPE, text=True,
                    timeout=timeout_s, check=False,
                )  # fmt: skip
                exit_code, stderr, timed_out = proc.returncode, proc.stderr, False
            except subprocess.TimeoutExpired as e:
                exit_code, timed_out = -1, True
                stderr = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else ""
        (log_dir / "stderr.log").write_text(stderr or "")
        duration_s = time.monotonic() - started
        text = answer.read_text() if answer.is_file() else None
        return self.parse(events, exit_code, timed_out, duration_s, started_ns, text, stderr)

    def parse(
        self,
        events: Path,
        exit_code: int,
        timed_out: bool,
        duration_s: float,
        started_ns: int,
        answer: str | None,
        stderr: str = "",
    ) -> JudgeResult:
        session_id = error = failure = None
        completed = False
        usage: dict[str, Any] = {}
        commands = 0
        messages: list[str] = []
        for event in _jsonl(events):
            kind = event.get("type")
            item = event.get("item") or {}
            if kind == "thread.started":
                session_id = event.get("thread_id")
            elif kind == "turn.completed":
                usage, completed = event.get("usage") or {}, True
            elif kind == "turn.failed":
                failure = (event.get("error") or {}).get("message") or "turn failed"
            elif kind == "error":
                # Also reported for problems Codex recovers from, such as a dropped connection.
                error = event.get("message") or error
            elif kind == "item.completed" and item.get("type") == "command_execution":
                commands += 1
            elif kind == "item.completed" and item.get("type") == "agent_message":
                messages.append(item.get("text") or "")
        answer = answer if answer and answer.strip() else (messages[-1] if messages else None)

        if timed_out:
            status, error = TIMEOUT, "timed out"
        elif exit_code == 0 and completed and failure is None and answer:
            status, error = OK, None
        else:
            status = FAILED
            tail = (stderr or "").strip().splitlines()[-1:]
            error = (
                failure
                or error
                or (tail[0] if tail else f"codex exited {exit_code} without an answer")
            )

        session_file = self._session_file(session_id) if session_id else None
        model, quota = _session_info(session_file) if session_file else (None, {})
        cached = usage.get("cached_input_tokens")
        input_tokens = usage.get("input_tokens")
        return JudgeResult(
            status=status,
            exit_code=exit_code,
            duration_s=duration_s,
            started_ns=started_ns,
            session_id=session_id,
            model=model or self.model,
            error=error[:500] if error else None,
            answer=answer,
            # Codex counts cached input inside input_tokens.
            input_tokens=input_tokens - (cached or 0) if input_tokens is not None else None,
            output_tokens=usage.get("output_tokens"),
            cache_read_tokens=cached,
            commands=commands,
            quota_used_pct=quota,
            session_file=session_file,
        )

    def _session_file(self, session_id: str) -> Path | None:
        found = sorted(self.sessions_dir.rglob(f"rollout-*-{session_id}.jsonl"))
        if not found:
            logger.warning("no Codex session file for %s in %s", session_id, self.sessions_dir)
        return found[-1] if found else None

    def log_trace(self, result: JudgeResult, prompt: str) -> str:
        """A ``codex_review`` agent span with a ``tool_shell`` span per command the judge ran
        (from the session file, which has their times)."""
        span = mlflow.start_span_no_context(
            "codex_review",
            span_type=SpanType.AGENT,
            inputs={"prompt": prompt},
            attributes={
                "model": result.model or self.model,
                "reasoning_effort": self.effort,
                SpanAttributeKey.CHAT_USAGE: _span_usage(result),
            },
            metadata={SESSION_METADATA: result.session_id or ""},
            start_time_ns=result.started_ns,
        )
        for item, started_ms, completed_ms in _commands(result.session_file):
            command = item.get("command") or []
            tool = mlflow.start_span_no_context(
                "tool_shell",
                span_type=SpanType.TOOL,
                parent_span=span,
                inputs={"command": command[-1] if command else ""},
                attributes={"exit_code": item.get("exit_code")},
                start_time_ns=started_ms * 1_000_000,
            )
            tool.end(
                outputs={"output": (item.get("aggregated_output") or "")[:SPAN_OUTPUT_CHARS]},
                status="OK" if item.get("exit_code") == 0 else "ERROR",
                end_time_ns=(completed_ms or started_ms) * 1_000_000,
            )
        span.end(
            outputs={"answer": result.answer or ""},
            attributes={"status": result.status, "error": result.error or ""},
            status="OK" if result.status == OK else "ERROR",
            end_time_ns=result.started_ns + int(result.duration_s * 1e9),
        )
        mlflow.flush_trace_async_logging()
        return span.trace_id


def log_assessments(trace_id: str, review: rubric.Review, judge_model: str) -> None:
    """Attach each criterion's score and rationale to the judge's trace as LLM-judge feedback."""
    source = AssessmentSource(source_type=AssessmentSourceType.LLM_JUDGE, source_id=judge_model)
    for criterion in rubric.CRITERIA:
        mlflow.log_feedback(
            trace_id=trace_id,
            name=criterion.key,
            value=review.scores[criterion.key],
            rationale=review.rationales[criterion.key],
            source=source,
        )


def _jsonl(path: Path | None) -> list[dict[str, Any]]:
    """The JSON objects in a JSONL file, skipping anything else."""
    if path is None or not path.is_file():
        return []
    objects = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            objects.append(value)
    return objects


def _session_info(path: Path) -> tuple[str | None, dict[str, float]]:
    """The model a Codex session ran and the quota used after it (5-hour and weekly windows)."""
    model = None
    limits: dict[str, Any] = {}
    for record in _jsonl(path):
        payload = record.get("payload") or {}
        if record.get("type") == "turn_context":
            model = payload.get("model") or model
        elif payload.get("type") == "token_count":
            limits = payload.get("rate_limits") or limits
    quota = {}
    for key, window in (("primary", "5h"), ("secondary", "week")):
        used = (limits.get(key) or {}).get("used_percent")
        if isinstance(used, int | float):
            quota[window] = float(used)
    return model, quota


def _commands(path: Path | None) -> list[tuple[dict[str, Any], int, int | None]]:
    """The shell commands in a Codex session file, with their start and end times in ms."""
    commands = []
    for record in _jsonl(path):
        payload = record.get("payload") or {}
        item = payload.get("item") or {}
        started = payload.get("started_at_ms")
        if (
            payload.get("type") == "item_completed"
            and item.get("type") == "CommandExecution"
            and isinstance(started, int)
        ):
            commands.append((item, started, payload.get("completed_at_ms")))
    return commands


def _span_usage(result: JudgeResult) -> dict[str, int]:
    input_tokens = (result.input_tokens or 0) + (result.cache_read_tokens or 0)
    output_tokens = result.output_tokens or 0
    return {
        TokenUsageKey.INPUT_TOKENS: input_tokens,
        TokenUsageKey.OUTPUT_TOKENS: output_tokens,
        TokenUsageKey.TOTAL_TOKENS: input_tokens + output_tokens,
    }


def get(name: str, model: str | None = None) -> Judge:
    """The judge called ``name``, optionally on another model than its default."""
    if name == "codex":
        return CodexJudge(model or CODEX_MODEL)
    raise ValueError(f"unknown judge {name!r} (expected one of {JUDGES})")


JUDGES = ("codex",)
