import json
import subprocess
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import IO

import mlflow
import pytest
from mlflow import MlflowClient

from openweight import promptdb
from openweight.branches import Evaluation, git
from openweight.checks import CHECKOUT, Check
from openweight.harnesses.base import FAILED, OK, RATE_LIMITED, TIMEOUT, HarnessError, StepResult
from openweight.runner import Options, Runner, RunnerError, select_steps
from openweight.tracking import SESSION_METADATA, SUPERSEDED_TAG, Tracker

PROMPTDB = Path(__file__).parents[1] / "PromptDB.md"
EVALUATION = Evaluation("claude", "claude-sonnet-5-5", "high")


@pytest.fixture
def tracker(tracking_uri: str, monkeypatch: pytest.MonkeyPatch) -> Tracker:
    """A tracker on a fresh experiment in a local store; fluent mlflow logs traces there too."""
    client = MlflowClient(tracking_uri)
    experiment_id = client.create_experiment(f"test-{uuid.uuid4().hex[:8]}")
    monkeypatch.setenv("MLFLOW_TRACKING_URI", tracking_uri)
    monkeypatch.setenv("MLFLOW_EXPERIMENT_ID", experiment_id)
    monkeypatch.setenv("FAKE_TOKEN", "secret")
    monkeypatch.setenv("MLFLOW_ENABLE_ASYNC_TRACE_LOGGING", "false")
    return Tracker(experiment_id, client)


class FakeHarness:
    """Reads back the outcome the fake sandbox wrote as its 'event stream'."""

    name = "claude"
    credentials = ("FAKE_TOKEN",)
    traces_in_sandbox = True
    prompt_on_stdin = False

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.finished: list[Path] = []
        self.unavailable: str | None = None

    def check(self, evaluation: Evaluation) -> None:
        if self.unavailable:
            raise HarnessError(self.unavailable)

    def command(self, prompt: str, evaluation: Evaluation, session_id: str) -> list[str]:
        self.prompts.append(prompt)
        return ["agent", session_id]

    def finish(self, box: object, events: Path) -> None:
        self.finished.append(events)

    def parse(self, events: Path, exit_code: int) -> StepResult:
        data = json.loads(events.read_text())
        return StepResult(
            status=data["status"],
            exit_code=exit_code,
            session_id=data["session_id"],
            error=None if data["status"] == OK else "it broke",
            cost_usd=1.5,
            tool_calls=3,
            rate_limit_resets_at=data.get("resets_at"),
        )


class FakeSandbox:
    """Plays the agent: writes a file and commits it (unless the attempt's number is in
    ``uncommitted``), logs a trace for the session, reports a status.

    Checks exit with the next of ``check_exits`` (0 once they run out).
    """

    def __init__(
        self,
        workspace: Path,
        statuses: Sequence[str],
        uncommitted: Sequence[int] = (),
        check_exits: Sequence[int] = (),
    ) -> None:
        self.workspace = workspace
        self.statuses = list(statuses)
        self.uncommitted = set(uncommitted)
        self.check_exits = list(check_exits)
        self.calls: list[tuple[list[str], dict[str, str], float | None]] = []
        self.inputs: list[str | None] = []
        self.checks: list[tuple[list[str], str]] = []

    def up(self) -> list[str]:
        return []

    def exec(
        self,
        command: Sequence[str],
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        workdir: str = "/workspace",
        stdout: IO[str] | None = None,
        input: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if command[0] != "agent":  # a check, or copying the workspace for them
            if not command[-1].startswith("exec 2>&1"):
                return subprocess.CompletedProcess(command, 0, "", "")
            self.checks.append((list(command), workdir))
            code = self.check_exits.pop(0) if self.check_exits else 0
            return subprocess.CompletedProcess(command, code, f"check output {code}", None)
        self.calls.append((list(command), dict(env or {}), timeout))
        self.inputs.append(input)
        session_id = command[1]
        status = self.statuses.pop(0) if self.statuses else OK
        n = len(self.calls)
        (self.workspace / f"file-{n}.txt").write_text(f"attempt {n}\n")
        if n not in self.uncommitted:
            git(self.workspace, "add", "--all")
            git(self.workspace, "commit", "--quiet", "-m", f"feat: attempt {n}")
        with mlflow.start_span(name="claude_code_conversation"):
            mlflow.update_current_trace(metadata={SESSION_METADATA: session_id})
        assert stdout is not None
        stdout.write(json.dumps({"status": status, "session_id": session_id, "resets_at": None}))
        return subprocess.CompletedProcess(command, 0 if status == OK else 1, None, "warn\n")


def make_runner(
    repo: Path,
    tracker: Tracker,
    statuses: Sequence[str] = (),
    uncommitted: Sequence[int] = (),
    check_list: Sequence[Check] = (),
    check_exits: Sequence[int] = (),
    **options: object,
) -> tuple[Runner, FakeSandbox, list[float]]:
    sleeps: list[float] = []
    db = promptdb.load(PROMPTDB)
    runner = Runner(
        repo, EVALUATION, db, FakeHarness(), None, tracker,  # type: ignore[arg-type]
        Options(trace_wait_s=5, **options), log=lambda _: None, sleep=sleeps.append,
        check_list=check_list,
    )  # fmt: skip
    box = FakeSandbox(runner.workspace.path, statuses, uncommitted, check_exits)
    runner.box = box  # type: ignore[assignment]
    return runner, box, sleeps


def runs(tracker: Tracker, role: str) -> list[mlflow.entities.Run]:
    return tracker.client.search_runs(
        [tracker.experiment_id],
        f"tags.`openweight.role` = '{role}'",
        order_by=["attributes.start_time ASC"],
    )


def test_run_logs_steps_to_mlflow(repo: Path, tracker: Tracker) -> None:
    runner, box, _ = make_runner(repo, tracker)
    outcomes = runner.run([1, 2])
    assert [o.status for o in outcomes] == [OK, OK]

    workspace = runner.workspace
    assert workspace.next_step() == 3
    assert git(repo, "rev-parse", "refs/tags/eval/claude/claude-sonnet-5-5-high/step-02").strip()
    command, env, timeout = box.calls[0]
    assert box.inputs == [None, None]  # the prompt is in the command
    assert env["FAKE_TOKEN"] == "secret"
    assert env["MLFLOW_EXPERIMENT_ID"] == tracker.experiment_id
    assert timeout == 3600

    [parent] = runs(tracker, "branch")
    assert parent.info.status == "FINISHED"
    assert parent.data.tags["branch"] == EVALUATION.branch
    assert parent.data.tags["harness_version"].startswith("claude ")
    steps = runs(tracker, "step")
    assert [r.info.run_name for r in steps] == ["step-01", "step-02"]
    first = steps[0]
    assert first.info.status == "FINISHED"
    assert first.data.tags["mlflow.parentRunId"] == parent.info.run_id
    assert first.data.tags["status"] == OK
    assert first.data.tags["harness_status"] == OK
    assert first.data.tags["session_id"] == command[1]
    assert first.data.params["title"] == "Scaffold the SvelteKit wetlands app"
    metrics = first.data.metrics
    assert metrics["cost_usd"] == 1.5
    assert metrics["agent_commits"] == 1
    assert metrics["agent_committed"] == 1
    assert metrics["files_changed"] == 1
    assert metrics["lines_added"] == 1
    assert metrics["traces"] == 1
    assert metrics["rate_limited_retries"] == 0
    assert metrics["retried"] == 0
    assert metrics["commits_conventional"] == 1
    assert metrics["commits_assisted_by"] == 0  # the fake agent writes no trailer
    assert "duration_s" in metrics
    assert not any(key.startswith("check") for key in metrics)  # no checks configured

    artifacts = {a.path for a in tracker.client.list_artifacts(first.info.run_id)}
    assert artifacts == {"prompt.md", "step-01.jsonl", "step-01.stderr.log", "diff.patch"}
    diff = tracker.client.download_artifacts(first.info.run_id, "diff.patch")
    assert "+attempt 1" in Path(diff).read_text()

    # The step commit carries the run id; the trace is linked to the run and tagged.
    assert workspace.step_commits()[0].run_id == first.info.run_id
    [trace] = tracker.client.search_traces(
        locations=[tracker.experiment_id], run_id=first.info.run_id, include_spans=False
    )
    assert trace.info.tags["step"] == "1"
    assert trace.info.tags["branch"] == EVALUATION.branch


def test_failed_step_stops(repo: Path, tracker: Tracker) -> None:
    runner, box, _ = make_runner(repo, tracker, [FAILED, FAILED], on_failure="stop")
    outcomes = runner.run([1, 2])
    assert [o.status for o in outcomes] == [FAILED]
    assert outcomes[0].retried
    assert len(box.calls) == 2
    [step] = runs(tracker, "step")
    assert step.info.status == "FAILED"
    assert step.data.tags["error"] == "it broke"
    assert runner.workspace.next_step() == 2  # the failed step is still recorded


def test_failed_step_continues(repo: Path, tracker: Tracker) -> None:
    runner, box, _ = make_runner(repo, tracker, [FAILED, FAILED])
    outcomes = runner.run([1, 2])
    assert [o.status for o in outcomes] == [FAILED, OK]
    assert len(box.calls) == 3


@pytest.mark.parametrize(
    ("status", "reason"),
    [(FAILED, "The session failed: it broke"), (TIMEOUT, "stopped at the 60-minute limit")],
)
def test_failed_step_is_retried_once(
    repo: Path, tracker: Tracker, status: str, reason: str
) -> None:
    runner, box, _ = make_runner(repo, tracker, [status, OK])
    [outcome] = runner.run([1])
    assert outcome.result.status == OK
    assert outcome.retried
    assert len(box.calls) == 2

    # The retry is a fresh session on top of the first attempt's work, told what went wrong.
    first_prompt, retry_prompt = runner.harness.prompts  # type: ignore[attr-defined]
    assert retry_prompt.startswith(first_prompt.rstrip())
    assert "## Retry" in retry_prompt and reason in retry_prompt
    if status == FAILED:
        assert "```\nwarn\n```" in retry_prompt  # the stderr tail
    files = git(runner.workspace.path, "ls-files").split()
    assert "file-1.txt" in files and "file-2.txt" in files
    assert outcome.record.agent_commits == 2

    [step] = runs(tracker, "step")
    assert step.info.status == "FINISHED"
    metrics = step.data.metrics
    assert metrics["retried"] == 1
    assert metrics["cost_usd"] == 3.0  # both attempts
    assert metrics["tool_calls"] == 6
    assert metrics["traces"] == 2
    tags = step.data.tags
    assert tags["status"] == OK
    assert tags["session_id"] == box.calls[0][0][1]
    assert tags["retry_session_id"] == box.calls[1][0][1]
    assert tags["retry_reason"] == "it broke"
    assert "error" not in tags
    artifacts = {a.path for a in tracker.client.list_artifacts(step.info.run_id)}
    assert {"retry-prompt.md", "step-01.jsonl", "step-01.retry.jsonl"} <= artifacts


def test_rate_limited_retry_keeps_first_attempt(repo: Path, tracker: Tracker) -> None:
    # The first attempt fails leaving an uncommitted file; the retry hits a usage limit once.
    runner, box, sleeps = make_runner(
        repo, tracker, [FAILED, RATE_LIMITED, OK], uncommitted=[1], rate_limit_wait_s=600
    )
    [outcome] = runner.run([1])
    assert outcome.result.status == OK
    assert outcome.retried
    assert outcome.rate_limited_retries == 1
    assert sleeps == [600]
    assert len(box.calls) == 3
    # The rate-limited retry's commit was discarded; the first attempt's uncommitted file was not.
    files = git(runner.workspace.path, "ls-files").split()
    assert "file-1.txt" in files and "file-2.txt" not in files and "file-3.txt" in files
    assert outcome.record.agent_commits == 1


def test_retry_can_be_turned_off(repo: Path, tracker: Tracker) -> None:
    runner, box, _ = make_runner(repo, tracker, [FAILED], retry_failed=False)
    [outcome] = runner.run([1])
    assert outcome.result.status == FAILED
    assert not outcome.retried
    assert len(box.calls) == 1


def test_exhausted_usage_limit_is_not_retried(repo: Path, tracker: Tracker) -> None:
    runner, box, _ = make_runner(repo, tracker, [RATE_LIMITED], max_rate_limit_retries=0)
    [outcome] = runner.run([1])
    assert outcome.result.status == RATE_LIMITED
    assert not outcome.retried
    assert len(box.calls) == 1


def test_rate_limited_step_is_reset_and_retried(repo: Path, tracker: Tracker) -> None:
    runner, box, sleeps = make_runner(repo, tracker, [RATE_LIMITED, OK], rate_limit_wait_s=600)
    [outcome] = runner.run([1])
    assert outcome.result.status == OK
    assert outcome.rate_limited_retries == 1
    assert sleeps == [600]
    assert len(box.calls) == 2
    # The rate-limited attempt's commit was discarded.
    files = git(runner.workspace.path, "ls-files").split()
    assert "file-1.txt" not in files and "file-2.txt" in files
    assert outcome.record.agent_commits == 1
    [step] = runs(tracker, "step")
    assert step.data.metrics["rate_limited_retries"] == 1


def test_rerun_needs_reset(repo: Path, tracker: Tracker) -> None:
    runner, _, _ = make_runner(repo, tracker)
    runner.run([1, 2])
    with pytest.raises(RunnerError, match="already ran"):
        runner.run([2])
    with pytest.raises(RunnerError, match="next step .* is 3, not 4"):
        runner.run([4])

    runner.run([2], reset=True)
    steps = runs(tracker, "step")
    assert [r.info.run_name for r in steps] == ["step-01", "step-02", "step-02"]
    assert steps[1].data.tags.get(SUPERSEDED_TAG) == "true"
    assert SUPERSEDED_TAG not in steps[2].data.tags
    assert runner.workspace.step_commits()[-1].run_id == steps[2].info.run_id
    assert len(runs(tracker, "branch")) == 1  # the parent run is reused


def test_prompt_on_stdin_and_no_tracking_env(repo: Path, tracker: Tracker) -> None:
    # Like OpenCode: the prompt goes in on stdin, and tracing happens on the host.
    runner, box, _ = make_runner(repo, tracker)
    runner.harness.prompt_on_stdin = True  # type: ignore[attr-defined]
    runner.harness.traces_in_sandbox = False  # type: ignore[attr-defined]
    runner.run([1])
    [prompt] = runner.harness.prompts  # type: ignore[attr-defined]
    assert box.inputs == [prompt]
    _, env, _ = box.calls[0]
    assert env == {"FAKE_TOKEN": "secret"}
    assert runner.harness.finished == [runner.runs_dir / "step-01.jsonl"]  # type: ignore[attr-defined]


def test_unavailable_model(repo: Path, tracker: Tracker) -> None:
    runner, box, _ = make_runner(repo, tracker)
    runner.harness.unavailable = "lumen does not serve glm-9"  # type: ignore[attr-defined]
    with pytest.raises(RunnerError, match="does not serve glm-9"):
        runner.run([1])
    assert box.calls == []
    assert runs(tracker, "branch") == []


def test_missing_credentials(repo: Path, tracker: Tracker, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FAKE_TOKEN")
    runner, _, _ = make_runner(repo, tracker)
    with pytest.raises(RunnerError, match="FAKE_TOKEN not set"):
        runner.run([1])


@pytest.mark.parametrize(
    ("spec", "until", "expected"),
    [
        ("next", None, [3]),
        ("next", 5, [3, 4, 5]),
        ("1", None, [1]),
        ("1-3", None, [1, 2, 3]),
        ("2", 4, [2, 3, 4]),
    ],
)
def test_select_steps(spec: str, until: int | None, expected: list[int]) -> None:
    assert select_steps(spec, until, next_step=3, last=42) == expected


@pytest.mark.parametrize(
    ("spec", "until", "message"),
    [
        ("x", None, "must be"),
        ("3-", None, "must be"),
        ("4-2", None, "no steps"),
        ("0", None, "no steps"),
        ("40-43", None, "only 42"),
        ("next", 2, "no steps"),
    ],
)
def test_select_steps_rejects(spec: str, until: int | None, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        select_steps(spec, until, next_step=3, last=42)


def test_tracker_branch_run_per_promptdb_revision(tracker: Tracker) -> None:
    first = tracker.branch_run(EVALUATION, "claude 1", "sha-a")
    tracker.end(first)
    assert tracker.branch_run(EVALUATION, "claude 2", "sha-a") == first
    run = tracker.client.get_run(first)
    assert run.info.status == "RUNNING"
    assert run.data.tags["harness_version"] == "claude 2"
    assert tracker.branch_run(EVALUATION, "claude 2", "sha-b") != first


UNIT = Check("unit", "unit tests", "make test", "web")
LINT = Check("lint", "lint", "make lint")


def test_checks_run_on_the_step(repo: Path, tracker: Tracker) -> None:
    later = Check("later", "later", "x", from_step=9)
    runner, box, _ = make_runner(
        repo, tracker, uncommitted=[1], check_list=[LINT, UNIT, later], retry_failed=False
    )
    [outcome] = runner.run([1])
    # Only lint ran: web/ does not exist, so unit fails without running; `later` is skipped.
    assert outcome.status == FAILED
    assert outcome.result.status == OK
    [(command, workdir)] = box.checks
    assert command == ["bash", "-c", "exec 2>&1\nmake lint"] and workdir == CHECKOUT
    assert [(r.check.name, r.status) for r in outcome.checks] == [
        ("lint", "passed"), ("unit", "failed"), ("later", "skipped"),
    ]  # fmt: skip

    [step] = runs(tracker, "step")
    metrics = step.data.metrics
    assert metrics["check.lint"] == 1
    assert metrics["check.unit"] == 0
    assert "check.later" not in metrics
    assert metrics["checks_passed"] == 1 and metrics["checks_failed"] == 1
    artifacts = {a.path for a in tracker.client.list_artifacts(step.info.run_id, "step-01.checks")}
    assert artifacts == {
        "step-01.checks/checks.json",
        "step-01.checks/lint.log",
        "step-01.checks/unit.log",
    }
    log = (runner.runs_dir / "step-01.checks" / "unit.log").read_text()
    assert log == "not run: web/ does not exist\n"


def test_failed_check_is_retried_once(repo: Path, tracker: Tracker) -> None:
    runner, box, _ = make_runner(repo, tracker, check_list=[LINT], check_exits=[1, 0])
    [outcome] = runner.run([1])
    assert outcome.status == OK
    assert outcome.retried
    assert len(box.calls) == 2 and len(box.checks) == 2

    _, retry_prompt = runner.harness.prompts  # type: ignore[attr-defined]
    assert "## Retry" in retry_prompt
    assert "### lint: `make lint`\n\n```\n$ make lint" in retry_prompt
    assert "check output 1" in retry_prompt

    [step] = runs(tracker, "step")
    assert step.data.metrics["check.lint"] == 1  # the final attempt's
    assert step.data.tags["retry_reason"] == "checks failed: lint"
    artifacts = {a.path for a in tracker.client.list_artifacts(step.info.run_id)}
    assert {"step-01.checks", "step-01.retry.checks", "retry-prompt.md"} <= artifacts


def test_check_still_failing_after_retry_fails_the_step(repo: Path, tracker: Tracker) -> None:
    runner, box, _ = make_runner(repo, tracker, check_list=[LINT], check_exits=[1, 1, 1])
    outcomes = runner.run([1, 2])
    assert [o.status for o in outcomes] == [FAILED, OK]
    assert [o.result.status for o in outcomes] == [OK, OK]
    # Step 2's lint failure is not new, so it neither retries nor fails the step.
    assert [o.retried for o in outcomes] == [True, False]
    assert len(box.checks) == 3

    first, second = runs(tracker, "step")
    assert first.info.status == "FAILED"
    assert first.data.tags["status"] == FAILED
    assert first.data.tags["harness_status"] == OK
    assert first.data.tags["error"] == "checks failed: lint"
    assert second.info.status == "FINISHED"
    assert second.data.metrics["check.lint"] == 0


def test_failed_session_is_checked_after_its_retry(repo: Path, tracker: Tracker) -> None:
    runner, box, _ = make_runner(repo, tracker, [FAILED, OK], check_list=[LINT])
    [outcome] = runner.run([1])
    assert outcome.status == OK
    assert len(box.checks) == 1  # only after the retry
    assert (runner.runs_dir / "step-01.retry.checks" / "lint.log").exists()


def test_check_failure_without_retry(repo: Path, tracker: Tracker) -> None:
    runner, box, _ = make_runner(
        repo, tracker, check_list=[LINT], check_exits=[1], retry_failed=False
    )
    [outcome] = runner.run([1])
    assert outcome.status == FAILED
    assert not outcome.retried
    assert len(box.checks) == 1
