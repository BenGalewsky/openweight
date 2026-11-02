"""Compare eval branches (``openweight report``).

Each branch's step metrics (notional cost, time, check results) are rolled up into PromptDB's
phases and set next to each phase's review scores, as a Markdown summary table over all branches
and a per-phase table for each. Steps are the ones the branch has now (their runs are found by the
``Eval-Run-Id`` trailers), and reviews the latest of the code it has now (see
`reviewer.current_reviews`).
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from openweight import branches
from openweight.promptdb import PromptDB
from openweight.review import rubric
from openweight.review.reviewer import Phase, current_reviews, phases, step_runs
from openweight.tracking import Tracker

OK = "ok"


@dataclass
class PhaseRow:
    phase: Phase
    steps: int = 0
    failed: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0
    checks_passed: int = 0
    checks_failed: int = 0
    # Each judge's scores by criterion.
    scores: dict[str, dict[str, float]] = field(default_factory=dict)

    def judge_mean(self, judge: str) -> float | None:
        scores = self.scores.get(judge)
        return sum(scores.values()) / len(scores) if scores else None

    def criterion(self, key: str) -> float | None:
        """``key``'s score averaged over the judges that scored it."""
        values = [scores[key] for scores in self.scores.values() if key in scores]
        return sum(values) / len(values) if values else None

    @property
    def mean(self) -> float | None:
        means = [m for judge in self.scores if (m := self.judge_mean(judge)) is not None]
        return sum(means) / len(means) if means else None


@dataclass
class BranchReport:
    branch: str
    harness: str
    model: str
    phases: list[PhaseRow]
    # Steps the branch has whose MLflow run was not found: counted, but not in the totals.
    untracked: list[int] = field(default_factory=list)

    @property
    def reviewed(self) -> list[PhaseRow]:
        return [row for row in self.phases if row.scores]

    def total(self, name: str) -> float:
        return sum(getattr(row, name) for row in self.phases)

    def criterion(self, key: str) -> float | None:
        """``key``'s score averaged over the reviewed phases."""
        values = [v for row in self.phases if (v := row.criterion(key)) is not None]
        return sum(values) / len(values) if values else None

    @property
    def mean(self) -> float | None:
        values = [m for row in self.phases if (m := row.mean) is not None]
        return sum(values) / len(values) if values else None


def collect(repo: Path, db: PromptDB, tracker: Tracker, branch: str) -> BranchReport:
    """``branch``'s steps and reviews, rolled up by phase."""
    rows = {phase.number: PhaseRow(phase) for phase in phases(db)}
    runs = step_runs(repo, tracker, branch)
    harness = model = ""
    untracked = []
    for number in sorted({c.step for c in branches.step_commits(repo, branch)}):
        if number > len(db.steps):
            continue
        row = rows[db.step(number).phase]
        row.steps += 1
        run = runs.get(number)
        if run is None:
            untracked.append(number)
            continue
        metrics, tags = run.data.metrics, run.data.tags
        harness, model = tags.get("harness", harness), tags.get("model", model)
        row.failed += tags.get("status") != OK
        row.cost_usd += metrics.get("cost_usd", 0.0)
        row.duration_s += metrics.get("duration_s", 0.0)
        row.checks_passed += int(metrics.get("checks_passed", 0))
        row.checks_failed += int(metrics.get("checks_failed", 0))
    for (phase, judge), run in current_reviews(repo, db, tracker, branch).items():
        if phase in rows:
            rows[phase].scores[judge] = {
                c.key: run.data.metrics[f"review.{c.key}"]
                for c in rubric.CRITERIA
                if f"review.{c.key}" in run.data.metrics
            }
    return BranchReport(branch, harness, model, list(rows.values()), untracked)


def render(reports: Sequence[BranchReport]) -> str:
    """The reports as Markdown: a summary table, then each branch's phases."""
    judges = sorted({judge for r in reports for row in r.phases for judge in row.scores})
    criteria = [c.title for c in rubric.CRITERIA]
    summary = ["Branch", "Steps", "Failed", "Cost", "Time", "Checks", "Reviewed", *criteria, "Mean"]
    lines = [
        "# openweight report",
        "",
        "Cost is notional: Claude Code's API-equivalent estimate on a subscription, or OpenCode's "
        "from the provider's per-token prices. Checks is the share of check runs that passed. "
        "Scores are 1-5, averaged over the judges and then over the reviewed phases.",
        "",
        _row(summary),
        _row(["---"] * len(summary)),
    ]
    for report in reports:
        reviewed = len(report.reviewed)
        lines.append(
            _row(
                [
                    f"`{report.branch}`",
                    str(int(report.total("steps"))),
                    str(int(report.total("failed"))),
                    _cost(report.total("cost_usd")),
                    _hours(report.total("duration_s")),
                    _share(report.total("checks_passed"), report.total("checks_failed")),
                    f"{reviewed} phase{'s' if reviewed != 1 else ''}",
                    *(_score(report.criterion(c.key)) for c in rubric.CRITERIA),
                    _score(report.mean),
                ]
            )
        )
    for report in reports:
        lines += [
            "",
            f"## `{report.branch}`",
            "",
            f"{report.harness} with `{report.model}`." if report.model else "No steps logged.",
            "",
        ]
        if report.untracked:
            steps = ", ".join(map(str, report.untracked))
            lines += [
                f"**Steps without an MLflow run, left out of the totals: {steps}.**",
                "",
            ]
        lines += [
            _row(["Phase", "Steps", "Failed", "Cost", "Time", "Checks", *judges, "Mean"]),
            _row(["---"] * (7 + len(judges))),
        ]
        for row in report.phases:
            phase = row.phase
            lines.append(
                _row(
                    [
                        f"{phase.number}. {phase.title} ({phase.first_step}-{phase.last_step})",
                        f"{row.steps}/{phase.last_step - phase.first_step + 1}",
                        str(row.failed),
                        _cost(row.cost_usd) if row.steps else "",
                        _hours(row.duration_s) if row.steps else "",
                        _share(row.checks_passed, row.checks_failed),
                        *(_score(row.judge_mean(judge)) for judge in judges),
                        _score(row.mean),
                    ]
                )
            )
    return "\n".join(lines) + "\n"


def _row(cells: Sequence[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _cost(usd: float) -> str:
    return f"${usd:,.2f}"


def _hours(seconds: float) -> str:
    return f"{seconds / 3600:.1f} h"


def _share(passed: float, failed: float) -> str:
    total = passed + failed
    return f"{passed / total:.0%}" if total else ""


def _score(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else ""
