"""Parse PromptDB.md into ordered steps and build the prompt handed to the agent for each step.

PromptDB.md is plain text, not Markdown: sections start at lines such as
``Phase 1 — Web app foundation (SQLite era)`` and ``Step 1 — Scaffold the app (459fbc1)``.
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

PHASE_RE = re.compile(r"^Phase (?P<number>\d+) — (?P<title>.+)$")
STEP_RE = re.compile(
    r"^Step (?P<number>\d+) — (?P<title>.+) \((?P<shas>[0-9a-f]{7,40}(?:, [0-9a-f]{7,40})*)\)$"
)
CONVENTIONS_START = "Conventions for every step"
CONVENTIONS_END = "Repo layout the steps converge on:"
NOT_YET_COMMITTED = "Not yet committed"
CAVEATS = "Caveats"

STEP_43_TITLE = "Uncommitted follow-up work"
STEP_43_PHASE_TITLE = "Follow-up"

FRAMEWORK_PREAMBLE = """\
You are implementing one step of a larger build. The repository already contains the work of all
previous steps; read it and its git history as needed before you start, and implement only this
step. The commit SHAs in parentheses after the step title (and any other SHA mentioned) refer to the
original project's history, which is not available to you. They are informational only."""

STEP_NOTES = {
    3: """\
plans/access_db_schema.sql has already been supplied: it is an mdb-schema dump of
Wetlands_IDOT_BE.accdb. Verify it against the Access databases at the repository root rather than
regenerating it, and correct it only where it is wrong or incomplete.""",
}


class PromptDBError(ValueError):
    """PromptDB.md does not have the expected structure."""


@dataclass(frozen=True)
class Step:
    number: int
    title: str
    phase: int
    phase_title: str
    source_commits: tuple[str, ...]
    body: str

    @property
    def heading(self) -> str:
        if not self.source_commits:
            return f"Step {self.number} — {self.title}"
        return f"Step {self.number} — {self.title} ({', '.join(self.source_commits)})"


@dataclass(frozen=True)
class PromptDB:
    conventions: str
    steps: tuple[Step, ...]
    sha256: str

    def step(self, number: int) -> Step:
        for step in self.steps:
            if step.number == number:
                return step
        raise KeyError(f"no step {number} (steps are 1-{len(self.steps)})")

    def build_prompt(self, step: Step, harness: str, model: str) -> str:
        conventions = self.conventions.replace("<harness>:<model>", f"{harness}:{model}")
        parts = [FRAMEWORK_PREAMBLE]
        if step.number in STEP_NOTES:
            parts.append(STEP_NOTES[step.number])
        parts.append(conventions)
        parts.append(f"{step.heading}\n\n{step.body}")
        return "\n\n".join(parts) + "\n"


def load(path: Path, include_step_43: bool = False) -> PromptDB:
    data = path.read_bytes()
    return parse(data.decode("utf-8"), hashlib.sha256(data).hexdigest(), include_step_43)


def parse(text: str, sha256: str, include_step_43: bool = False) -> PromptDB:
    lines = text.splitlines()
    conventions = _parse_conventions(lines)

    steps: list[Step] = []
    phase: tuple[int, str] | None = None
    header: re.Match[str] | None = None
    body: list[str] = []
    follow_up: list[str] | None = None

    def finish_step() -> None:
        if header is None:
            return
        if phase is None:
            raise PromptDBError(f"step {header['number']} appears before any phase heading")
        steps.append(
            Step(
                number=int(header["number"]),
                title=header["title"],
                phase=phase[0],
                phase_title=phase[1],
                source_commits=tuple(header["shas"].split(", ")),
                body=_join_body(body, f"step {header['number']}"),
            )
        )

    for line in lines:
        if line.strip() == CAVEATS:
            break
        if line.startswith(NOT_YET_COMMITTED):
            finish_step()
            header = None
            follow_up = []
            continue
        if follow_up is not None:
            follow_up.append(line)
            continue
        if match := PHASE_RE.match(line):
            finish_step()
            header = None
            phase = (int(match["number"]), match["title"])
        elif match := STEP_RE.match(line):
            finish_step()
            header = match
            body = []
        elif header is not None:
            body.append(line)
    finish_step()

    _check_numbering(steps)
    if include_step_43:
        if follow_up is None:
            raise PromptDBError(f"no '{NOT_YET_COMMITTED}' section for step 43")
        steps.append(
            Step(
                number=len(steps) + 1,
                title=STEP_43_TITLE,
                phase=steps[-1].phase + 1,
                phase_title=STEP_43_PHASE_TITLE,
                source_commits=(),
                body=_join_body(follow_up, "step 43"),
            )
        )
    return PromptDB(conventions=conventions, steps=tuple(steps), sha256=sha256)


def _parse_conventions(lines: list[str]) -> str:
    start = next((i for i, line in enumerate(lines) if line.startswith(CONVENTIONS_START)), None)
    end = next((i for i, line in enumerate(lines) if line.startswith(CONVENTIONS_END)), None)
    if start is None or end is None or end <= start:
        raise PromptDBError("conventions section not found")
    items = [line.strip() for line in lines[start + 1 : end] if line.strip()]
    if not items:
        raise PromptDBError("conventions section is empty")
    return "Conventions for every step:\n\n" + "\n".join(f"- {item}" for item in items)


def _join_body(lines: list[str], what: str) -> str:
    body = "\n".join(line.rstrip() for line in lines).strip()
    body = re.sub(r"\n{3,}", "\n\n", body)
    if not body:
        raise PromptDBError(f"{what} has an empty body")
    return body


def _check_numbering(steps: list[Step]) -> None:
    if not steps:
        raise PromptDBError("no steps found")
    numbers = [step.number for step in steps]
    expected = list(range(1, len(steps) + 1))
    if numbers != expected:
        raise PromptDBError(f"steps are not numbered consecutively from 1: {numbers}")
