import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from mlflow.entities import Run

from openweight import branches, promptdb, report
from openweight.branches import Evaluation, git
from openweight.review import judge as judges
from openweight.review import rubric
from openweight.review.judge import CodexJudge, JudgeResult
from openweight.review.reviewer import (
    Phase,
    Reviewer,
    ReviewError,
    current_reviews,
    phases,
    select_phases,
)
from openweight.tracking import SUPERSEDED_TAG, Tracker

PROMPTDB = Path(__file__).parents[1] / "PromptDB.md"
EVALUATION = Evaluation("opencode", "lumen/glm-5.3")


def answer(score: int = 4) -> dict[str, Any]:
    return {
        **{c.key: {"score": score, "rationale": f"{c.key} is fine"} for c in rubric.CRITERIA},
        "top_issues": [{"file": "web/a.ts:3", "issue": "off by one"}],
    }


class FakeJudge:
    """Answers with the next of ``answers`` (a dict, or a JudgeResult to return as is), keeping
    what it was shown."""

    name = "codex"
    model = "gpt-test"

    def __init__(self, *answers: dict[str, Any] | JudgeResult) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []
        self.inputs: list[dict[str, str]] = []
        self.code: list[dict[str, str]] = []
        self.writable: list[bool] = []

    def run(self, prompt: str, code: Path, log_dir: Path, timeout_s: float) -> JudgeResult:
        self.prompts.append(prompt)
        inputs = Path(prompt.split("inputs are in ")[1].split(":")[0])
        self.inputs.append(_files(inputs))
        self.code.append(_files(code))
        self.writable.append(any(os.access(p, os.W_OK) for p in code.rglob("*") if p.is_file()))
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "events.jsonl").write_text("{}\n")
        reply = self.answers.pop(0) if self.answers else answer()
        if isinstance(reply, JudgeResult):
            return reply
        return JudgeResult(
            status=judges.OK, exit_code=0, duration_s=90, started_ns=time.time_ns(),
            session_id="judge-session", model="gpt-test-1", answer=json.dumps(reply),
            input_tokens=100, output_tokens=20, cache_read_tokens=50, commands=4,
            quota_used_pct={"5h": 3.0, "week": 1.0},
        )  # fmt: skip

    def log_trace(self, result: JudgeResult, prompt: str) -> str:
        return CodexJudge(self.model).log_trace(result, prompt)


def _files(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): p.read_text(errors="replace")
        for p in root.rglob("*")
        if p.is_file()
    }


def make_branch(repo: Path, tracker: Tracker, last_step: int) -> str:
    """An eval branch with steps 1..last_step, each with a logged step run. Step 14 fails a check
    and step 15's work is committed by the runner."""
    db = promptdb.load(PROMPTDB)
    workspace = branches.open_workspace(repo, EVALUATION)
    parent = tracker.branch_run(EVALUATION, "opencode 2", db.sha256)
    for n in range(1, last_step + 1):
        run_id = tracker.start_step(parent, EVALUATION, db.step(n), db.sha256, 60)
        (workspace.path / "web").mkdir(exist_ok=True)
        (workspace.path / "web" / f"step{n}.ts").write_text(f"export const step = {n};\n")
        (workspace.path / "web" / "package-lock.json").write_text(f'{{"step": {n}}}\n')
        if n != 15:
            git(workspace.path, "add", "--all")
            message = (
                f"feat: step {n}\n\nWritten by lumen/glm-5.3 in OpenCode for the step.\n\n"
                "Assisted-by: opencode:lumen/glm-5.3\nCo-authored-by: GLM\n  <glm@example.com>"
            )
            git(workspace.path, "commit", "--quiet", "-m", message)
        workspace.record_step(n, run_id)
        tracker.log_metrics(
            run_id,
            {
                "cost_usd": 2.0, "duration_s": 600, "check.web_install": 1.0,
                "check.web_lint": float(n != 14), "checks_passed": 1 + (n != 14),
                "checks_failed": float(n == 14), "retried": float(n == 14),
            },
        )  # fmt: skip
        tracker.set_tags(run_id, {"status": "failed" if n == 14 else "ok"})
        tracker.end(run_id)
    tracker.end(parent)
    branches.sync_to_main(repo, workspace)
    return EVALUATION.branch


def reviews(tracker: Tracker) -> list[Run]:
    return tracker.client.search_runs(
        [tracker.experiment_id],
        "tags.`openweight.role` = 'review'",
        order_by=["attributes.start_time ASC"],
    )


def make_reviewer(repo: Path, tracker: Tracker, judge: FakeJudge) -> Reviewer:
    return Reviewer(repo, promptdb.load(PROMPTDB), tracker, [judge], log=lambda _: None)


def test_phases_follow_promptdb() -> None:
    found = [(p.number, p.first_step, p.last_step) for p in phases(promptdb.load(PROMPTDB))]
    assert found == [(1, 1, 13), (2, 14, 16), (3, 17, 20), (4, 21, 29), (5, 30, 35),
                     (6, 36, 38), (7, 39, 42)]  # fmt: skip


ALL = [Phase(1, "one", 1, 3), Phase(2, "two", 4, 5), Phase(3, "three", 6, 9)]


@pytest.mark.parametrize(
    ("spec", "reviewed", "expected"),
    [(None, set(), [1, 2]), (None, {1}, [2]), ("2", {2}, [2]), ("1-2", set(), [1, 2])],
)
def test_select_phases(spec: str | None, reviewed: set[int], expected: list[int]) -> None:
    assert [p.number for p in select_phases(spec, ALL, 5, reviewed)] == expected


@pytest.mark.parametrize(
    ("spec", "message"),
    [("3", "phase 3 is not complete"), ("0", "no phases"), ("2-1", "no phases"), ("x", "N or N-M")],
)
def test_select_phases_rejects(spec: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        select_phases(spec, ALL, 5, set())


def _strict(schema: dict[str, Any]) -> None:
    if schema.get("type") == "object":
        assert schema["additionalProperties"] is False
        assert sorted(schema["required"]) == sorted(schema["properties"])
        for value in schema["properties"].values():
            _strict(value)
    elif schema.get("type") == "array":
        _strict(schema["items"])


def test_schema_suits_codex_output_schema() -> None:
    _strict(rubric.SCHEMA)


def test_parse_review() -> None:
    review = rubric.parse_review(json.dumps(answer(3) | {"correctness": {"score": 1,
                                                         "rationale": "broken"}}))  # fmt: skip
    assert review.scores["correctness"] == 1
    assert review.metrics()["review.correctness"] == 1
    assert review.mean == pytest.approx((3 * 5 + 1) / 6)
    assert review.issues == (rubric.Issue("web/a.ts:3", "off by one"),)
    assert "## Correctness: 1/5\n\nbroken" in review.markdown("Phase 1")


@pytest.mark.parametrize(
    "text",
    ["not json", "[]", json.dumps(answer() | {"process": {"score": 6, "rationale": ""}}),
     json.dumps({k: v for k, v in answer().items() if k != "process"}),
     json.dumps(answer() | {"process": {"score": 3}}),
     json.dumps({k: v for k, v in answer().items() if k != "top_issues"}),
     json.dumps(answer() | {"top_issues": [{"file": "a.ts"}]})],
)  # fmt: skip
def test_parse_review_rejects(text: str) -> None:
    with pytest.raises(rubric.RubricError):
        rubric.parse_review(text)


def test_review_logs_each_complete_phase(repo: Path, tracker: Tracker) -> None:
    branch = make_branch(repo, tracker, 16)
    judge = FakeJudge()
    outcomes = make_reviewer(repo, tracker, judge).review(branch)
    assert [o.phase.number for o in outcomes] == [1, 2]  # phase 3 is not complete

    first, second = reviews(tracker)
    parent = tracker.find_branch_run(branch, promptdb.load(PROMPTDB).sha256)
    assert parent is not None
    tags = second.data.tags
    assert second.info.run_name == "phase-2-review"
    assert second.info.status == "FINISHED"
    assert tags["mlflow.parentRunId"] == parent.info.run_id
    assert (tags["phase"], tags["first_step"], tags["last_step"]) == ("2", "14", "16")
    assert tags["commit"] == git(repo, "rev-parse", f"refs/tags/{branch}/step-16").strip()
    assert (tags["judge"], tags["judge_model"], tags["status"]) == ("codex", "gpt-test-1", "ok")
    assert tags["model"] == "lumen/glm-5.3"
    metrics = second.data.metrics
    assert metrics["review.completeness"] == 4
    assert metrics["review.mean"] == 4
    assert metrics["judge_quota_5h_pct"] == 3
    assert metrics["judge_input_tokens"] == 100

    artifacts = {a.path for a in tracker.client.list_artifacts(second.info.run_id)}
    assert {"prompt.md", "review.json", "rationales.md", "judge"} <= artifacts
    judge_files = {a.path for a in tracker.client.list_artifacts(second.info.run_id, "judge")}
    assert "judge/inputs" in judge_files
    local = repo / "runs" / branch / "review" / "phase-2.codex"
    assert (local / "inputs" / "checks.md").is_file()

    [trace] = tracker.client.search_traces(
        locations=[tracker.experiment_id], run_id=second.info.run_id
    )
    assert trace.info.tags["phase"] == "2"
    feedback = {a.name: a for a in trace.info.assessments}
    assert set(feedback) == {c.key for c in rubric.CRITERIA}
    assert feedback["process"].feedback.value == 4
    assert feedback["process"].rationale == "process is fine"
    assert feedback["process"].source.source_id == "gpt-test-1"

    # Already reviewed: nothing left to do.
    assert make_reviewer(repo, tracker, judge).review(branch) == []
    assert first.data.tags["phase"] == "1"


def test_judge_inputs_are_blind(repo: Path, tracker: Tracker) -> None:
    branch = make_branch(repo, tracker, 16)
    judge = FakeJudge()
    make_reviewer(repo, tracker, judge).review(branch, "2")
    [prompt], [inputs], [code], [writable] = judge.prompts, judge.inputs, judge.code, judge.writable

    assert "phase 2, " in prompt and "(steps 14-16)" in prompt
    assert "do not count them as missing" in prompt  # the stripped Assisted-by trailers
    assert not writable
    assert "web/step16.ts" in code and "web/step17.ts" not in code
    assert not any(path.startswith(".git") for path in code)
    assert set(inputs) == {
        "prompts.md", "changes.txt", "commits.txt", "checks.md", "diff/web/step14.ts.diff",
        "diff/web/step15.ts.diff", "diff/web/step16.ts.diff",
    }  # fmt: skip
    assert "Step 14 — " in inputs["prompts.md"] and "Step 13 — " not in inputs["prompts.md"]
    assert "Assisted-by: <harness>:<model>" in inputs["prompts.md"]
    assert "(lock file: no diff)" in inputs["changes.txt"]
    commits = inputs["commits.txt"]
    assert "feat: step 14" in commits
    assert "Written by <model> in <agent> for the step." in commits
    assert "example.com" not in commits  # the folded trailer's continuation line
    assert "made by the evaluation framework" in commits  # step 15
    for text in inputs.values():
        assert "glm" not in text.lower()
        assert "opencode" not in text.lower()
        assert "Assisted-by: opencode" not in text
        assert "Eval-" not in text
    checks = inputs["checks.md"]
    assert "Step status: failed, after a retry" in checks
    assert "Failed: web lint" in checks


def test_explicit_phase_is_reviewed_again(repo: Path, tracker: Tracker) -> None:
    branch = make_branch(repo, tracker, 13)
    make_reviewer(repo, tracker, FakeJudge(answer(2), answer(5))).review(branch)
    make_reviewer(repo, tracker, FakeJudge(answer(5))).review(branch, "1")
    old, new = reviews(tracker)
    assert old.data.tags[SUPERSEDED_TAG] == "true"
    assert SUPERSEDED_TAG not in new.data.tags
    db = promptdb.load(PROMPTDB)
    assert current_reviews(repo, db, tracker, branch)[(1, "codex")].info.run_id == new.info.run_id


def test_branch_without_runs_has_nothing_to_review(repo: Path, tracker: Tracker) -> None:
    branches.sync_to_main(repo, branches.open_workspace(repo, EVALUATION))
    assert make_reviewer(repo, tracker, FakeJudge()).review(EVALUATION.branch) == []


def test_incomplete_phase_is_refused(repo: Path, tracker: Tracker) -> None:
    branch = make_branch(repo, tracker, 14)
    with pytest.raises(ValueError, match="phase 2 is not complete"):
        make_reviewer(repo, tracker, FakeJudge()).review(branch, "2")


def test_failed_judge_session(repo: Path, tracker: Tracker) -> None:
    branch = make_branch(repo, tracker, 13)
    failed = JudgeResult(
        status=judges.FAILED, exit_code=1, duration_s=5, started_ns=time.time_ns(),
        error="usage limit reached",
    )  # fmt: skip
    with pytest.raises(ReviewError, match="usage limit reached"):
        make_reviewer(repo, tracker, FakeJudge(failed)).review(branch)
    [run] = reviews(tracker)
    assert run.info.status == "FAILED"
    assert run.data.tags["status"] == "error"
    assert current_reviews(repo, promptdb.load(PROMPTDB), tracker, branch) == {}


def test_answer_off_the_rubric(repo: Path, tracker: Tracker) -> None:
    branch = make_branch(repo, tracker, 13)
    with pytest.raises(ReviewError, match="no score from 1 to 5 for completeness"):
        make_reviewer(repo, tracker, FakeJudge({"completeness": {}})).review(branch)


def test_reset_supersedes_reviews_from_its_phase(repo: Path, tracker: Tracker) -> None:
    branch = make_branch(repo, tracker, 16)
    make_reviewer(repo, tracker, FakeJudge()).review(branch)
    superseded = tracker.supersede_reviews(branch, 2)
    phase1, phase2 = reviews(tracker)
    assert superseded == [phase2.info.run_id]
    assert SUPERSEDED_TAG not in phase1.data.tags


def test_report(repo: Path, tracker: Tracker) -> None:
    branch = make_branch(repo, tracker, 16)
    make_reviewer(repo, tracker, FakeJudge(answer(4), answer(2))).review(branch)
    db = promptdb.load(PROMPTDB)
    collected = report.collect(repo, db, tracker, branch)
    phase1, phase2, phase3 = collected.phases[:3]
    assert (phase1.steps, phase1.cost_usd, phase1.failed) == (13, 26.0, 0)
    assert (phase2.steps, phase2.failed, phase2.checks_failed) == (3, 1, 1)
    assert phase2.scores == {"codex": {c.key: 2.0 for c in rubric.CRITERIA}}
    assert (phase3.steps, phase3.scores) == (0, {})
    assert collected.mean == 3
    assert collected.model == "lumen/glm-5.3"

    text = report.render([collected])
    assert "without an MLflow run" not in text
    assert f"| `{branch}` | 16 | 1 | $32.00 | 2.7 h | 97% | 2 phases |" in text
    assert "| 2. " in text and "| 3/3 | 1 | $6.00 | 0.5 h | 83% | 2.00 | 2.00 |" in text


def test_report_flags_steps_without_runs(repo: Path, tracker: Tracker) -> None:
    branch = make_branch(repo, tracker, 3)
    tracker.client.delete_run(branches.step_commits(repo, branch)[1].run_id)
    collected = report.collect(repo, promptdb.load(PROMPTDB), tracker, branch)
    assert (collected.phases[0].steps, collected.phases[0].cost_usd) == (3, 4.0)
    assert collected.untracked == [2]
    assert "Steps without an MLflow run, left out of the totals: 2." in report.render([collected])


EVENTS = [
    {"type": "thread.started", "thread_id": "t-123"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"type": "command_execution", "command": "cat a"}},
    {"type": "item.completed", "item": {"type": "agent_message", "text": '{"a": 1}'}},
    {"type": "turn.completed", "usage": {"input_tokens": 1000, "cached_input_tokens": 600,
                                         "output_tokens": 50}},
]  # fmt: skip


def _session_file(sessions: Path) -> None:
    path = sessions / "2026" / "11" / "02" / "rollout-2026-11-02T10-00-00-t-123.jsonl"
    path.parent.mkdir(parents=True)
    records = [
        {"type": "turn_context", "payload": {"model": "gpt-6.1-sol"}},
        {"type": "event_msg", "payload": {
            "type": "item_completed", "started_at_ms": 1_000, "completed_at_ms": 2_000,
            "item": {"type": "CommandExecution", "command": ["/bin/zsh", "-lc", "cat a"],
                     "aggregated_output": "hello", "exit_code": 0}}},
        {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
            "primary": {"used_percent": 3.0}, "secondary": {"used_percent": 0.5}}}},
    ]  # fmt: skip
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def test_codex_command() -> None:
    command = CodexJudge().command(Path("/c"), Path("/s.json"), Path("/a.json"))
    assert command[:2] == ["codex", "exec"]
    assert command[-1] == "-"  # the prompt comes on stdin
    for flag, value in [
        ("--sandbox", "read-only"),
        ("--cd", "/c"),
        ("--model", "gpt-6.1-sol"),
        ("--output-schema", "/s.json"),
        ("--output-last-message", "/a.json"),
    ]:
        assert command[command.index(flag) + 1] == value  # fmt: skip
    assert "model_reasoning_effort=high" in command
    assert "--json" in command and "--ignore-user-config" in command


def test_codex_parse(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text("".join(json.dumps(e) + "\n" for e in EVENTS) + "not json\n")
    _session_file(tmp_path / "sessions")
    judge = CodexJudge(sessions_dir=tmp_path / "sessions")
    result = judge.parse(events, 0, False, 12.0, 0, '{"b": 2}')
    assert result.status == judges.OK
    assert result.session_id == "t-123"
    assert result.model == "gpt-6.1-sol"
    assert result.answer == '{"b": 2}'
    assert (result.input_tokens, result.cache_read_tokens, result.output_tokens) == (400, 600, 50)
    assert result.commands == 1
    assert result.quota_used_pct == {"5h": 3.0, "week": 0.5}
    assert result.metrics()["judge_quota_week_pct"] == 0.5

    # No answer file: the last agent message is the answer.
    assert judge.parse(events, 0, False, 1.0, 0, None).answer == '{"a": 1}'

    # An error Codex recovered from does not fail the session.
    recovered = [{"type": "error", "message": "Reconnecting... 1/5"}, *EVENTS]
    events.write_text("".join(json.dumps(e) + "\n" for e in recovered))
    assert judge.parse(events, 0, False, 1.0, 0, '{"b": 2}').status == judges.OK


def test_codex_parse_failures(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    failed = [
        {"type": "thread.started", "thread_id": "t-9"},
        {"type": "turn.failed", "error": {"message": "You've hit your usage limit."}},
    ]
    events.write_text("".join(json.dumps(e) + "\n" for e in failed))
    judge = CodexJudge(sessions_dir=tmp_path)
    result = judge.parse(events, 1, False, 1.0, 0, None)
    assert (result.status, result.error) == (judges.FAILED, "You've hit your usage limit.")
    assert judge.parse(events, -1, True, 1.0, 0, None).status == judges.TIMEOUT
    events.write_text("")
    result = judge.parse(events, 2, False, 1.0, 0, None, stderr="boom\nauth failed\n")
    assert (result.status, result.error) == (judges.FAILED, "auth failed")


def test_codex_trace(tmp_path: Path, tracker: Tracker) -> None:
    _session_file(tmp_path / "sessions")
    events = tmp_path / "events.jsonl"
    events.write_text("".join(json.dumps(e) + "\n" for e in EVENTS))
    judge = CodexJudge(sessions_dir=tmp_path / "sessions")
    result = judge.parse(events, 0, False, 12.0, time.time_ns(), '{"b": 2}')
    trace = tracker.client.get_trace(judge.log_trace(result, "review this"))
    spans = {span.name: span for span in trace.data.spans}
    assert set(spans) == {"codex_review", "tool_shell"}
    assert spans["tool_shell"].inputs == {"command": "cat a"}
    assert spans["tool_shell"].start_time_ns == 1_000_000_000
    assert trace.info.trace_metadata["mlflow.trace.session"] == "t-123"
