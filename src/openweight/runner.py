"""Run benchmark steps: one fresh harness session per step, in the evaluation's sandbox.

For each step the runner checks the workspace is clean at the previous step's tag, starts the
sandbox, runs the harness under a timeout with its event stream captured to
``runs/<branch>/step-NN.jsonl``, records the step commit and tag, syncs the branch to the main
repository, and logs everything to a child run of the branch's MLflow run, with the harness's
traces linked to it. A session that fails or times out gets one retry: a fresh session that
starts from the work the first one left, told what went wrong.
"""

import os
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from openweight import branches, sandbox, tracking
from openweight.branches import Evaluation, StepRecord, Workspace
from openweight.harnesses.base import FAILED, OK, RATE_LIMITED, TIMEOUT, Harness, StepResult
from openweight.promptdb import PromptDB, Step
from openweight.sandbox import Sandbox
from openweight.tracking import Tracker

ON_FAILURE = ("continue", "stop")
# Margin after a usage limit's reported reset time before the step is retried.
RATE_LIMIT_MARGIN_S = 60
# Statuses that earn a step its one retry. A usage limit is not the agent's failure: those
# sessions are rerun until the limit resets instead.
RETRIED_STATUSES = (FAILED, TIMEOUT)
# How much of a failed session's stderr the retry prompt quotes.
STDERR_TAIL_LINES = 40


class RunnerError(RuntimeError):
    """The requested steps cannot be run."""


@dataclass(frozen=True)
class Options:
    timeout_s: float = 60 * 60
    on_failure: str = "continue"
    # How long to wait after hitting a usage limit when the harness does not say when it resets.
    rate_limit_wait_s: float = 15 * 60
    max_rate_limit_retries: int = 20
    trace_wait_s: float = 60
    # Give a failed or timed-out step one more session.
    retry_failed: bool = True

    def __post_init__(self) -> None:
        if self.on_failure not in ON_FAILURE:
            raise ValueError(f"on_failure must be one of {ON_FAILURE}, not {self.on_failure!r}")


@dataclass(frozen=True)
class StepOutcome:
    step: int
    run_id: str
    result: StepResult
    record: StepRecord
    duration_s: float
    rate_limited_retries: int = 0
    trace_ids: list[str] = field(default_factory=list)
    retried: bool = False


@dataclass(frozen=True)
class Attempt:
    """One session of a step, after any usage-limit reruns."""

    result: StepResult
    duration_s: float
    rate_limited_retries: int
    events: Path
    stderr: Path


def retry_prompt(prompt: str, reason: str) -> str:
    """``prompt`` with a note asking a fresh session to finish a step an earlier one failed."""
    note = (
        "## Retry\n\n"
        "An earlier session working on this step did not finish. Its work is in the repository as "
        "it left it: any commits it made, and any uncommitted changes in the working tree. Review "
        "that work, then complete the step and fix what went wrong.\n\n"
        f"{reason}"
    )
    return f"{prompt.rstrip()}\n\n{note}\n"


def select_steps(spec: str, until: int | None, next_step: int, last: int) -> list[int]:
    """The steps named by ``spec`` (``next``, ``N``, or ``N-M``), or from ``spec``'s first step
    through ``until``."""
    if spec == "next":
        first = end = next_step
    else:
        start, sep, stop = spec.partition("-")
        if not start.isdigit() or (sep and not stop.isdigit()):
            raise ValueError(f"steps must be 'next', N, or N-M, not {spec!r}")
        first, end = int(start), int(stop) if sep else int(start)
    if until is not None:
        end = until
    if first < 1 or end < first:
        raise ValueError(f"no steps in {spec!r}" + (f" until {until}" if until else ""))
    if end > last:
        raise ValueError(f"there are only {last} steps")
    return list(range(first, end + 1))


class Runner:
    def __init__(
        self,
        repo: Path,
        evaluation: Evaluation,
        db: PromptDB,
        harness: Harness,
        box: Sandbox,
        tracker: Tracker,
        options: Options | None = None,
        log: Callable[[str], None] = print,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.repo = repo
        self.evaluation = evaluation
        self.db = db
        self.harness = harness
        self.box = box
        self.tracker = tracker
        self.options = options or Options()
        self.log = log
        self.sleep = sleep
        self.runs_dir = repo / "runs" / evaluation.branch
        self._workspace: Workspace | None = None

    @property
    def workspace(self) -> Workspace:
        if self._workspace is None:
            self._workspace = branches.open_workspace(self.repo, self.evaluation)
        return self._workspace

    def env(self) -> dict[str, str]:
        missing = [key for key in self.harness.credentials if not os.environ.get(key)]
        if missing:
            raise RunnerError(f"{', '.join(missing)} not set (see .env.example)")
        credentials = {key: os.environ[key] for key in self.harness.credentials}
        return credentials | tracking.harness_env()

    def run(self, steps: Sequence[int], reset: bool = False) -> list[StepOutcome]:
        """Run ``steps`` (consecutive, starting at the branch's next step) in order.

        With ``reset``, steps that already ran from ``steps[0]`` on are discarded first and their
        MLflow runs marked superseded.
        """
        env = self.env()
        workspace = self.workspace
        next_step = workspace.next_step()
        if steps[0] > next_step:
            raise RunnerError(f"the next step on {workspace.branch} is {next_step}, not {steps[0]}")
        if steps[0] < next_step:
            if not reset:
                raise RunnerError(
                    f"step {steps[0]} already ran on {workspace.branch} (next is {next_step}); "
                    f"pass --reset to discard steps {steps[0]}-{next_step - 1} and rerun"
                )
            self._reset(steps[0])

        parent = self.tracker.branch_run(
            self.evaluation, sandbox.harness_version(self.evaluation.harness), self.db.sha256
        )
        outcomes = []
        try:
            for number in steps:
                outcome = self.run_step(self.db.step(number), parent, env)
                outcomes.append(outcome)
                if outcome.result.status != OK and self.options.on_failure == "stop":
                    self.log(f"stopping after step {number} ({outcome.result.status})")
                    break
        except BaseException:
            self.tracker.end(parent, "KILLED")
            raise
        self.tracker.end(parent)
        return outcomes

    def _reset(self, step: int) -> None:
        workspace = self.workspace
        superseded = [c.run_id for c in workspace.step_commits() if c.step >= step and c.run_id]
        self.log(f"resetting {workspace.branch} to {self.evaluation.tag(step - 1)}")
        workspace.reset_to(step - 1)
        branches.sync_to_main(self.repo, workspace)
        self.tracker.supersede(superseded)

    def run_step(self, step: Step, parent: str, env: dict[str, str]) -> StepOutcome:
        workspace = self.workspace
        workspace.ensure_ready(step.number)
        for action in self.box.up():
            self.log(action)

        prompt = self.db.build_prompt(step, self.evaluation.harness, self.evaluation.model)
        run_id = self.tracker.start_step(
            parent, self.evaluation, step, self.db.sha256, self.options.timeout_s
        )
        try:
            return self._run_step(step, prompt, run_id, env)
        except BaseException as e:
            self.tracker.set_tags(run_id, {"status": "error", "error": str(e)[:500]})
            self.tracker.end(run_id, "KILLED" if isinstance(e, KeyboardInterrupt) else "FAILED")
            raise

    def _run_step(self, step: Step, prompt: str, run_id: str, env: dict[str, str]) -> StepOutcome:
        workspace = self.workspace
        name = f"step-{step.number:02d}"
        self.tracker.log_text(run_id, prompt, "prompt.md")
        self.runs_dir.mkdir(parents=True, exist_ok=True)

        attempts = [
            self._attempt(step, prompt, name, env, lambda: workspace.reset_to(step.number - 1))
        ]
        first = attempts[0].result
        if first.status in RETRIED_STATUSES and self.options.retry_failed:
            reason = self._failure_reason(attempts[0])
            self.log(f"step {step.number}: {first.status}; retrying once in a fresh session")
            again = retry_prompt(prompt, reason)
            self.tracker.log_text(run_id, again, "retry-prompt.md")
            checkpoint = workspace.checkpoint()
            attempts.append(
                self._attempt(
                    step, again, f"{name}.retry", env, lambda: workspace.restore(checkpoint)
                )
            )
        result = attempts[-1].result

        record = workspace.record_step(step.number, run_id)
        branches.sync_to_main(self.repo, workspace)

        for attempt in attempts:
            self.tracker.log_artifact(run_id, attempt.events)
            if attempt.stderr.stat().st_size:
                self.tracker.log_artifact(run_id, attempt.stderr)
        self.tracker.log_text(run_id, workspace.diff(step.number), "diff.patch")

        trace_tags = {"branch": self.evaluation.branch, "step": str(step.number)}
        trace_ids: list[str] = []
        for attempt in attempts:
            if attempt.result.session_id:
                # A killed session never reaches the Stop hook that writes the trace.
                timed_out = attempt.result.status == TIMEOUT
                wait_s = 0 if timed_out else self.options.trace_wait_s
                trace_ids += self.tracker.link_traces(
                    run_id, attempt.result.session_id, trace_tags, wait_s
                )

        duration_s = sum(attempt.duration_s for attempt in attempts)
        rate_limited_retries = sum(attempt.rate_limited_retries for attempt in attempts)
        metrics: dict[str, float] = {}
        for attempt in attempts:
            for key, value in attempt.result.metrics().items():
                metrics[key] = metrics.get(key, 0) + value
        metrics |= workspace.diff_stats(step.number)
        metrics |= {
            "duration_s": duration_s,
            "exit_code": result.exit_code,
            "agent_commits": record.agent_commits,
            "agent_committed": float(record.agent_committed),
            "rate_limited_retries": rate_limited_retries,
            "retried": float(len(attempts) > 1),
            "traces": len(trace_ids),
        }
        self.tracker.log_metrics(run_id, metrics)
        tags = {
            "status": result.status,
            "session_id": first.session_id or "",
            "commit": record.sha,
            "step_tag": record.tag,
        }
        if len(attempts) > 1:
            tags["retry_session_id"] = result.session_id or ""
            tags["retry_reason"] = (first.error or first.status)[:500]
        if result.error:
            tags["error"] = result.error
        self.tracker.set_tags(run_id, tags)
        self.tracker.end(run_id, "FINISHED" if result.status == OK else "FAILED")

        outcome = StepOutcome(
            step.number, run_id, result, record, duration_s, rate_limited_retries, trace_ids,
            retried=len(attempts) > 1,
        )  # fmt: skip
        self.log(_summary(outcome))
        return outcome

    def _attempt(
        self, step: Step, prompt: str, name: str, env: dict[str, str], restore: Callable[[], None]
    ) -> Attempt:
        """Run one session, rerunning it from the state ``restore`` returns to while it hits a
        usage limit."""
        events = self.runs_dir / f"{name}.jsonl"
        stderr = self.runs_dir / f"{name}.stderr.log"
        retries = 0
        while True:
            session_id = str(uuid.uuid4())
            command = self.harness.command(prompt, self.evaluation, session_id)
            self.log(
                f"step {step.number} — {step.title}: running {self.harness.name} "
                f"(timeout {self.options.timeout_s / 60:g} min)"
            )
            started = time.monotonic()
            with events.open("w") as out:
                proc = self.box.exec(command, env=env, timeout=self.options.timeout_s, stdout=out)
            duration_s = time.monotonic() - started
            stderr.write_text(proc.stderr or "")
            result = self.harness.parse(events, proc.returncode)
            if result.status != RATE_LIMITED or retries >= self.options.max_rate_limit_retries:
                return Attempt(result, duration_s, retries, events, stderr)
            retries += 1
            wait_s = self.options.rate_limit_wait_s
            if result.rate_limit_resets_at is not None:
                wait_s = max(result.rate_limit_resets_at - time.time(), 0) + RATE_LIMIT_MARGIN_S
            self.log(
                f"step {step.number}: usage limit reached; resetting and retrying in "
                f"{wait_s / 60:.0f} min (retry {retries})"
            )
            restore()
            self.sleep(wait_s)

    def _failure_reason(self, attempt: Attempt) -> str:
        """What went wrong with ``attempt``, for the retry prompt."""
        if attempt.result.status == TIMEOUT:
            return f"The session was stopped at the {self.options.timeout_s / 60:g}-minute limit."
        reason = f"The session failed: {attempt.result.error or 'no error reported'}"
        lines = attempt.stderr.read_text().strip().splitlines()[-STDERR_TAIL_LINES:]
        if lines:
            tail = "\n".join(lines)
            reason += f"\n\nThe last lines it wrote to stderr:\n\n```\n{tail}\n```"
        return reason


def _summary(outcome: StepOutcome) -> str:
    result = outcome.result
    minutes, seconds = divmod(round(outcome.duration_s), 60)
    parts = [f"step {outcome.step}: {result.status} in {minutes}m{seconds:02d}s"]
    if outcome.retried:
        parts[0] += " after a retry"
    if result.cost_usd is not None:
        parts.append(f"${result.cost_usd:.2f} (notional)")
    parts.append(f"{outcome.record.agent_commits} agent commit(s)")
    parts.append(f"{len(outcome.trace_ids)} trace(s)")
    line = ", ".join(parts) + f" -> {outcome.record.tag}"
    if result.error:
        line += f"\n  error: {result.error}"
    return line
