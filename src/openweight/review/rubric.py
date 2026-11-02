"""The review rubric: the criteria a phase is scored on, the judge's prompt, and its JSON answer.

Every criterion is scored 1-5 with a written rationale, for the phase as a whole. The schema is
also given to the judge (Codex's ``--output-schema``), which needs ``additionalProperties: false``
and every property listed as required.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCORES = (1, 2, 3, 4, 5)


@dataclass(frozen=True)
class Criterion:
    key: str
    title: str
    description: str


CRITERIA = (
    Criterion(
        "completeness", "Completeness",
        "every requirement in the phase's step prompts is implemented.",
    ),
    Criterion(
        "correctness", "Correctness",
        "the code would work: no bugs, and the objective checks pass.",
    ),
    Criterion(
        "code_quality", "Code quality",
        "idiomatic Svelte 5 (runes) / SvelteKit / Drizzle / Dagster, readable, no dead code.",
    ),
    Criterion(
        "consistency", "Consistency",
        "fits the existing codebase and its conventions; no regressions of earlier work.",
    ),
    Criterion(
        "scope_discipline", "Scope discipline",
        "nothing the prompts did not ask for: no unrequested features, files or rewrites.",
    ),
    Criterion(
        "process", "Process",
        "commit messages follow the conventions in prompts.md and describe the work accurately; "
        "tests are added where a step asks for them.",
    ),
)  # fmt: skip

_SCORE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["score", "rationale"],
    "properties": {
        "score": {"type": "integer", "enum": list(SCORES)},
        "rationale": {"type": "string"},
    },
}
_ISSUE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["file", "issue"],
    "properties": {"file": {"type": "string"}, "issue": {"type": "string"}},
}
SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [*(c.key for c in CRITERIA), "top_issues"],
    "properties": {
        **{c.key: _SCORE for c in CRITERIA},
        "top_issues": {"type": "array", "items": _ISSUE},
    },
}

MAX_ISSUES = 10

PROMPT = """\
You are reviewing one phase of an AI coding agent's work on a benchmark build: the ISGS/IDOT
Wetlands Water Database, a SvelteKit + Drizzle web app and a Dagster ingestion backend, ported from
an MS Access database. The agent was given the phase's step prompts one at a time, each in a fresh
session, and committed its work after each step. You score the phase as a whole: {phase}.

You are read-only. Do not modify any file, install packages, start servers, or run the app, its
tests or its build. Read files and run read-only commands (ls, cat, sed, head, rg, find) only.

The code as the phase left it is the current working directory (it has no git history). The review
inputs are in {inputs}:
- prompts.md: the conventions every step was given, then the phase's step prompts in order.
- changes.txt: every file the phase changed, with the lines it added and removed.
- diff/: the phase's diff split by file: the diff of <path> is diff/<path>.diff. Lock files and
  binary files have none.
- commits.txt: the commit messages of each step, oldest first. A commit marked "made by the
  evaluation framework" holds work the agent left uncommitted, or records a step where it changed
  nothing. To keep you blind to which agent did the work, the Assisted-by trailers the
  conventions ask for, and any other trailers naming an agent or model, have been removed from
  every message: do not count them as missing.
- checks.md: the objective checks (install, typecheck, lint, build, database seed, ...) run after
  each step, and their results.

How to work:
1. Read prompts.md and list every requirement of every step in the phase.
2. Go through changes.txt file by file, reading each file's diff in diff/ in full, and the file
   itself where the diff alone is not enough. Do not skim or sample: a large phase needs this most.
3. Check each requirement against the code. Verify what the commit messages claim rather than
   trusting them.
4. Read checks.md: a check that fails is evidence about correctness.

Score each criterion from 1 to 5 (5 excellent, 4 good with minor issues, 3 acceptable with notable
gaps, 2 poor, 1 failing), with a rationale that cites files and lines:
{criteria}

In top_issues, list the most severe problems you found, most severe first (at most {max_issues}),
each with its file (and line) and what is wrong. Respond only with JSON matching the provided
schema.
"""


def judge_prompt(phase: str, inputs: Path) -> str:
    """The judge's prompt for ``phase`` (e.g. "phase 2, Docker and Postgres (steps 14-16)"), with
    the review inputs in ``inputs``."""
    criteria = "\n".join(f"- {c.key}: {c.description}" for c in CRITERIA)
    return PROMPT.format(phase=phase, inputs=inputs, criteria=criteria, max_issues=MAX_ISSUES)


class RubricError(ValueError):
    """The judge's answer does not match the rubric."""


@dataclass(frozen=True)
class Issue:
    file: str
    issue: str


@dataclass(frozen=True)
class Review:
    scores: dict[str, int]
    rationales: dict[str, str]
    issues: tuple[Issue, ...]

    @property
    def mean(self) -> float:
        return sum(self.scores.values()) / len(self.scores)

    def metrics(self) -> dict[str, float]:
        """``review.<criterion>`` for each criterion, and ``review.mean``."""
        values = {f"review.{key}": float(score) for key, score in self.scores.items()}
        return values | {"review.mean": self.mean}

    def to_json(self) -> dict[str, Any]:
        return {
            **{
                key: {"score": self.scores[key], "rationale": self.rationales[key]}
                for key in self.scores
            },
            "top_issues": [{"file": i.file, "issue": i.issue} for i in self.issues],
        }

    def markdown(self, heading: str) -> str:
        parts = [f"# {heading}"]
        for criterion in CRITERIA:
            parts.append(
                f"## {criterion.title}: {self.scores[criterion.key]}/5\n\n"
                f"{self.rationales[criterion.key]}"
            )
        if self.issues:
            issues = "\n".join(f"{n}. `{i.file}`: {i.issue}" for n, i in enumerate(self.issues, 1))
            parts.append(f"## Top issues\n\n{issues}")
        return "\n\n".join(parts) + "\n"


def parse_review(text: str) -> Review:
    """The judge's JSON answer as a `Review`, raising `RubricError` if it does not fit."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise RubricError(f"the judge's answer is not JSON: {e}") from None
    if not isinstance(data, dict):
        raise RubricError("the judge's answer is not a JSON object")
    scores, rationales = {}, {}
    for criterion in CRITERIA:
        entry = data.get(criterion.key)
        score = entry.get("score") if isinstance(entry, dict) else None
        if isinstance(score, bool) or score not in SCORES:
            raise RubricError(f"no score from 1 to 5 for {criterion.key}: {entry!r}")
        rationale = entry.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            raise RubricError(f"no rationale for {criterion.key}: {entry!r}")
        scores[criterion.key], rationales[criterion.key] = score, rationale
    issues = data.get("top_issues")
    if not isinstance(issues, list):
        raise RubricError(f"top_issues is not a list: {issues!r}")
    for issue in issues:
        if not (
            isinstance(issue, dict)
            and isinstance(issue.get("file"), str)
            and isinstance(issue.get("issue"), str)
        ):
            raise RubricError(f"top_issues has an entry without a file and an issue: {issue!r}")
    return Review(scores, rationales, tuple(Issue(i["file"], i["issue"]) for i in issues))
