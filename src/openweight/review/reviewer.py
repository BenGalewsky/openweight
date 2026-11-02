"""Review eval branches phase by phase (``openweight review``).

The unit of review is one of PromptDB's phases, once every step in it has run. For each phase and
judge the reviewer builds the judge's inputs in a fresh temporary directory, outside the main
repository so the judge cannot find its git history:

- ``code/``: the code at the phase's last step tag, exported from the main repository's tag (never
  the live workspace) without ``.git``, its files read-only.
- ``review/``: the phase's step prompts, its diff split by file, the commit messages of each step,
  and each step's check results (see `rubric.PROMPT`).

The judge is blind: the ``Assisted-by``, ``Co-authored-by`` and ``Eval-*`` trailers are stripped
from the commit messages, and the branch's model ids are replaced with ``<model>`` in every input
file (the exported code itself is left as the agent wrote it).

Each review is a child run of the branch's parent run, with the scores as ``review.<criterion>``
metrics, and the judge's trace linked to it carrying the scores as assessments. A local copy of
the inputs and the judge's logs goes to ``runs/<branch>/review/phase-N.<judge>/``.
"""

import io
import json
import re
import shutil
import stat
import tarfile
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from mlflow.entities import Run

from openweight import branches, checks
from openweight.promptdb import STEP_NOTES, PromptDB, Step
from openweight.review import judge as judges
from openweight.review import rubric
from openweight.review.judge import Judge, JudgeResult
from openweight.tracking import Tracker

LOCK_FILES = frozenset(
    {"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock", "bun.lockb", "uv.lock"}
)
# A changed file's diff is cut off after this much (the judge can read the file itself).
MAX_DIFF_BYTES = 200_000
REDACTED = "<model>"
HIDDEN_TRAILER = re.compile(r"^(assisted-by|co-authored-by|eval-[\w-]+)\s*:", re.IGNORECASE)
GENERATED_WITH = re.compile(r"generated with \[?(claude code|opencode)", re.IGNORECASE)
# Harness names, replaced in commit messages (the code may name them for other reasons, e.g.
# .claude/ settings, so it is left alone).
HARNESS_NAMES = re.compile(r"\b(claude code|claude|opencode|open code)\b", re.IGNORECASE)
REDACTED_HARNESS = "<agent>"


class ReviewError(RuntimeError):
    """A review cannot be run, or the judge did not produce one."""


@dataclass(frozen=True)
class Phase:
    number: int
    title: str
    first_step: int
    last_step: int

    def describe(self) -> str:
        return f"phase {self.number}, {self.title} (steps {self.first_step}-{self.last_step})"

    @property
    def heading(self) -> str:
        return f"Phase {self.number} — {self.title} (steps {self.first_step}-{self.last_step})"


@dataclass(frozen=True)
class ReviewOutcome:
    branch: str
    phase: Phase
    judge: str
    run_id: str
    result: JudgeResult
    review: rubric.Review


def phases(db: PromptDB) -> list[Phase]:
    """PromptDB's phases, in order, with the steps each covers."""
    found: dict[int, Phase] = {}
    for step in db.steps:
        phase = found.get(step.phase)
        first = phase.first_step if phase else step.number
        found[step.phase] = Phase(step.phase, step.phase_title, first, step.number)
    return list(found.values())


def select_phases(
    spec: str | None, all_phases: Sequence[Phase], last_step_run: int, reviewed: set[int]
) -> list[Phase]:
    """The phases named by ``spec`` (``N`` or ``N-M``), or by default every complete phase that
    has not been reviewed. A phase whose last step has not run is refused."""
    complete = [phase for phase in all_phases if phase.last_step <= last_step_run]
    if spec is None:
        return [phase for phase in complete if phase.number not in reviewed]
    start, sep, stop = spec.partition("-")
    if not start.isdigit() or (sep and not stop.isdigit()):
        raise ValueError(f"phases must be N or N-M, not {spec!r}")
    first, last = int(start), int(stop) if sep else int(start)
    numbers = [phase.number for phase in all_phases]
    if first > last or first not in numbers or last not in numbers:
        raise ValueError(f"no phases {spec!r} (phases are {numbers[0]}-{numbers[-1]})")
    selected = [phase for phase in all_phases if first <= phase.number <= last]
    for phase in selected:
        if phase.last_step > last_step_run:
            raise ValueError(
                f"phase {phase.number} is not complete: its last step, {phase.last_step}, has not "
                f"run (the branch is at step {last_step_run})"
            )
    return selected


class Reviewer:
    def __init__(
        self,
        repo: Path,
        db: PromptDB,
        tracker: Tracker,
        judge_list: Sequence[Judge],
        timeout_s: float = 30 * 60,
        log: Callable[[str], None] = print,
    ) -> None:
        self.repo = repo
        self.db = db
        self.tracker = tracker
        self.judges = list(judge_list)
        self.timeout_s = timeout_s
        self.log = log

    def review(self, branch: str, spec: str | None = None) -> list[ReviewOutcome]:
        """Review the phases of ``branch`` that ``spec`` selects (see `select_phases`) with each
        judge. An explicitly selected phase is reviewed again, superseding its earlier review."""
        last_step_run = branches.next_step(self.repo, branch) - 1
        outcomes = []
        reviewed = current_reviews(self.repo, self.db, self.tracker, branch)
        for judge in self.judges:
            current = {phase: run for (phase, name), run in reviewed.items() if name == judge.name}
            selected = select_phases(spec, phases(self.db), last_step_run, set(current))
            if not selected:
                self.log(f"{branch}: nothing to review with {judge.name}")
                continue
            parent = self.tracker.find_branch_run(branch, self.db.sha256)
            if parent is None:
                raise ReviewError(f"no MLflow run for {branch} at this PromptDB revision")
            for phase in selected:
                outcomes.append(self.review_phase(parent, branch, phase, judge))
                if phase.number in current:
                    self.tracker.supersede([current[phase.number].info.run_id])
        return outcomes

    def review_phase(self, parent: Run, branch: str, phase: Phase, judge: Judge) -> ReviewOutcome:
        commit = tag_sha(self.repo, branch, phase.last_step)
        if commit is None:
            raise ReviewError(f"no tag {branches.step_tag(branch, phase.last_step)}")
        log_dir = self.repo / "runs" / branch / "review" / f"phase-{phase.number}.{judge.name}"
        shutil.rmtree(log_dir, ignore_errors=True)
        run_id = self.tracker.start_review(
            parent, phase.number, (phase.first_step, phase.last_step), commit, judge.name,
            judge.model,
        )  # fmt: skip
        try:
            with tempfile.TemporaryDirectory(prefix="openweight-review-") as tmp:
                code, inputs = Path(tmp) / "code", Path(tmp) / "review"
                export_code(self.repo, branches.step_tag(branch, phase.last_step), code)
                runs = step_runs(self.repo, self.tracker, branch)
                write_inputs(
                    self.repo, self.db, branch, phase, runs, _redaction(parent, runs.values()),
                    inputs,
                )  # fmt: skip
                prompt = rubric.judge_prompt(phase.describe(), inputs)
                self.log(f"{branch}: reviewing {phase.describe()} with {judge.name}")
                result = judge.run(prompt, code, log_dir, self.timeout_s)
                shutil.copytree(inputs, log_dir / "inputs")
            return self._record(run_id, branch, phase, judge, prompt, result, log_dir)
        except BaseException as e:
            self.tracker.set_tags(run_id, {"status": "error", "error": str(e)[:500]})
            self.tracker.end(run_id, "KILLED" if isinstance(e, KeyboardInterrupt) else "FAILED")
            raise

    def _record(
        self, run_id: str, branch: str, phase: Phase, judge: Judge, prompt: str,
        result: JudgeResult, log_dir: Path,
    ) -> ReviewOutcome:  # fmt: skip
        self.tracker.log_text(run_id, prompt, "prompt.md")
        self.tracker.log_artifacts(run_id, log_dir, "judge")
        self.tracker.set_tags(
            run_id,
            {
                "judge_model": result.model or judge.model,
                "session_id": result.session_id or "",
                "judge_status": result.status,
            },
        )
        trace_id = judge.log_trace(result, prompt)
        trace_tags = {"branch": branch, "phase": str(phase.number), "judge": judge.name}
        self.tracker.link_trace_ids(run_id, [trace_id], trace_tags)
        metrics = result.metrics()
        if result.status != judges.OK:
            self.tracker.log_metrics(run_id, metrics)
            raise ReviewError(f"{judge.name} did not review phase {phase.number}: {result.error}")
        try:
            review = rubric.parse_review(result.answer or "")
        except rubric.RubricError as e:
            self.tracker.log_metrics(run_id, metrics)
            raise ReviewError(f"{judge.name}'s review of phase {phase.number}: {e}") from None

        judges.log_assessments(trace_id, review, result.model or judge.model)
        self.tracker.log_text(run_id, json.dumps(review.to_json(), indent=2) + "\n", "review.json")
        heading = f"{phase.heading}, reviewed by {result.model or judge.model}"
        self.tracker.log_text(run_id, review.markdown(heading), "rationales.md")
        self.tracker.log_metrics(run_id, metrics | review.metrics())
        self.tracker.set_tags(run_id, {"status": "ok"})
        self.tracker.end(run_id)
        scores = " ".join(f"{key} {score}" for key, score in review.scores.items())
        minutes, seconds = divmod(round(result.duration_s), 60)
        self.log(
            f"phase {phase.number}: {scores} (mean {review.mean:.2f}) in {minutes}m{seconds:02d}s, "
            f"{len(review.issues)} issue(s)"
        )
        return ReviewOutcome(branch, phase, judge.name, run_id, result, review)


def step_runs(repo: Path, tracker: Tracker, branch: str) -> dict[int, Run]:
    """The MLflow run of each step the branch has now, found by its ``Eval-Run-Id`` trailer."""
    runs = tracker.step_runs(branch)
    return {
        commit.step: runs[commit.run_id]
        for commit in branches.step_commits(repo, branch)
        if commit.run_id in runs
    }


def tag_sha(repo: Path, branch: str, step: int) -> str | None:
    """The commit of ``step``'s tag on ``branch`` in the main repository, if it has one."""
    tag = f"refs/tags/{branches.step_tag(branch, step)}"
    if not branches.has_ref(repo, tag):
        return None
    return branches.git(repo, "rev-parse", f"{tag}^{{commit}}").strip()


def current_reviews(
    repo: Path, db: PromptDB, tracker: Tracker, branch: str
) -> dict[tuple[int, str], Run]:
    """The latest finished review of each phase by each judge, keyed by (phase, judge), that
    judged the code the branch has now under this PromptDB revision."""
    found: dict[tuple[int, str], Run] = {}
    shas: dict[int, str | None] = {}
    for run in tracker.reviews(branch):
        tags = run.data.tags
        key = (int(tags.get("phase", "0")), tags.get("judge", ""))
        last_step = int(tags.get("last_step", "0"))
        if last_step not in shas:
            shas[last_step] = tag_sha(repo, branch, last_step)
        if (
            key not in found
            and run.info.status == "FINISHED"
            and tags.get("promptdb_sha") == db.sha256
            and tags.get("commit") == shas[last_step]
        ):
            found[key] = run
    return found


def _redaction(parent: Run, step_runs: Iterable[Run]) -> re.Pattern[str]:
    """Matches the model ids the branch ran with, and their names without the provider prefix."""
    models = {parent.data.tags.get("model", "")}
    models |= {run.data.tags.get("session_model", "") for run in step_runs}
    terms = {term for model in models if model for term in (model, model.rsplit("/", 1)[-1])}
    alternatives = "|".join(re.escape(term) for term in sorted(terms, key=len, reverse=True))
    return re.compile(alternatives or r"(?!)", re.IGNORECASE)


def export_code(repo: Path, ref: str, dest: Path) -> None:
    """Export the tree at ``ref`` to ``dest`` (no ``.git``), with its files made read-only."""
    archive = branches.run_git_bytes(repo, "archive", "--format=tar", f"refs/tags/{ref}")
    dest.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(dest, filter="data")
    for path in dest.rglob("*"):
        if path.is_file() and not path.is_symlink():
            path.chmod(path.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def write_inputs(
    repo: Path,
    db: PromptDB,
    branch: str,
    phase: Phase,
    step_runs: Mapping[int, Run],
    redact: re.Pattern[str],
    inputs: Path,
) -> None:
    """Write the judge's inputs for ``phase`` to ``inputs`` (see `rubric.PROMPT`)."""
    inputs.mkdir(parents=True)
    steps = [db.step(n) for n in range(phase.first_step, phase.last_step + 1)]

    def write(rel: str, text: str) -> None:
        path = inputs / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(redact.sub(REDACTED, text))

    prompts = [f"# {phase.heading}", db.conventions]
    for step in steps:
        prompts.append(f"## {step.heading}")
        if step.number in STEP_NOTES:
            prompts.append(STEP_NOTES[step.number])
        prompts.append(step.body)
    write("prompts.md", "\n\n".join(prompts) + "\n")

    span = (
        f"refs/tags/{branches.step_tag(branch, phase.first_step - 1)}"
        f"..refs/tags/{branches.step_tag(branch, phase.last_step)}"
    )
    changes = ["added removed  path"]
    numstat = branches.git(repo, "diff", "--numstat", "--no-renames", "-z", span)
    for entry in filter(None, numstat.split("\0")):
        added, removed, path = entry.split("\t", 2)
        binary = added == "-"
        note = "  (binary)" if binary else ""
        if Path(path).name in LOCK_FILES:
            note = "  (lock file: no diff)"
        changes.append(f"{added:>5} {removed:>7}  {path}{note}")
        if not note:
            diff = branches.git(
                repo, "--literal-pathspecs", "diff", "--no-renames", span, "--", path
            )
            if len(diff) > MAX_DIFF_BYTES:
                diff = (
                    diff[:MAX_DIFF_BYTES]
                    + f"\n[... diff cut off at {MAX_DIFF_BYTES} bytes; read {path} itself ...]\n"
                )
            write(f"diff/{path}.diff", diff)
    write("changes.txt", "\n".join(changes) + "\n")

    write("commits.txt", "\n\n".join(_step_commits(repo, branch, step) for step in steps) + "\n")
    titles = {check.metric: check.title for check in checks.CHECKS}
    write(
        "checks.md",
        "\n\n".join(_step_checks(step_runs.get(step.number), step, titles) for step in steps)
        + "\n",
    )


def _step_commits(repo: Path, branch: str, step: Step) -> str:
    span = (
        f"refs/tags/{branches.step_tag(branch, step.number - 1)}"
        f"..refs/tags/{branches.step_tag(branch, step.number)}"
    )
    out = branches.git(repo, "log", "--reverse", "--format=%an%x00%B%x1e", span)
    messages = []
    for record in out.split("\x1e"):
        author, sep, message = record.strip("\n").partition("\x00")
        if not sep:
            continue
        lines, hiding = [], False
        for line in message.strip().splitlines():
            # A trailer's value may be folded onto indented continuation lines.
            hiding = bool(HIDDEN_TRAILER.match(line)) or (hiding and line[:1] in (" ", "\t"))
            if not hiding and not GENERATED_WITH.search(line):
                lines.append(line)
        text = HARNESS_NAMES.sub(REDACTED_HARNESS, "\n".join(lines).strip())
        if author == branches.RUNNER_NAME:
            text += "\n\n(made by the evaluation framework, not the agent)"
        messages.append(text)
    return f"## Step {step.number} — {step.title}\n\n" + "\n\n---\n\n".join(messages)


def _step_checks(run: Run | None, step: Step, titles: Mapping[str, str]) -> str:
    heading = f"## Step {step.number} — {step.title}"
    if run is None:
        return f"{heading}\n\nNo record of this step's checks."
    metrics = run.data.metrics
    status = run.data.tags.get("status", "unknown")
    if metrics.get("retried"):
        status += ", after a retry in a fresh session"
    lines = [heading, "", f"Step status: {status}"]
    results = {key: value for key, value in metrics.items() if key in titles}
    if not results:
        lines.append("No checks ran (none apply yet, or checks were off).")
    for label, value in (("Passed", 1), ("Failed", 0)):
        names = [titles[key] for key in titles if results.get(key) == value]
        if names:
            lines.append(f"{label}: {', '.join(names)}")
    return "\n".join(lines)
