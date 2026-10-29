import hashlib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from openweight import promptdb
from openweight.cli import app

PROMPTDB = Path(__file__).parents[1] / "PromptDB.md"


@pytest.fixture(scope="module")
def db() -> promptdb.PromptDB:
    return promptdb.load(PROMPTDB)


def test_parses_all_42_steps(db: promptdb.PromptDB) -> None:
    assert [step.number for step in db.steps] == list(range(1, 43))
    assert all(step.title and step.body and step.source_commits for step in db.steps)


def test_step_headers(db: promptdb.PromptDB) -> None:
    first = db.step(1)
    assert first.title == "Scaffold the SvelteKit wetlands app"
    assert first.source_commits == ("459fbc1",)
    assert first.body.startswith("Create a SvelteKit 3.1 + Svelte 5 + TypeScript app in web/")
    assert db.step(5).source_commits == ("93aa2f1", "1c23b85", "91deb8f", "93e6494")
    assert db.step(15).title == "Migrate SQLite → PostgreSQL"


def test_phases(db: promptdb.PromptDB) -> None:
    phases = {step.phase: step.phase_title for step in db.steps}
    assert sorted(phases) == list(range(1, 8))
    assert phases[1] == "Web app foundation (SQLite era)"
    assert phases[7] == "CI and deployment"
    assert db.step(13).phase == 1
    assert db.step(14).phase == 2
    assert db.step(42).phase == 7


def test_bodies_stop_at_next_section(db: promptdb.PromptDB) -> None:
    for step in db.steps:
        assert "Phase " not in step.body.splitlines()[-1]
        assert "\nStep " not in step.body
    last = db.step(42).body
    assert "Not yet committed" not in last
    assert "Caveats" not in last
    assert "git show" not in last


def test_conventions(db: promptdb.PromptDB) -> None:
    assert "Assisted-by: <harness>:<model>" in db.conventions
    assert "DATABASE_URL=postgresql://idot:idot@localhost:5432/idot" in db.conventions
    assert "svelte-autofixer" in db.conventions
    assert "Repo layout" not in db.conventions
    assert "Reverted history" not in db.conventions


def test_sha256(db: promptdb.PromptDB) -> None:
    assert db.sha256 == hashlib.sha256(PROMPTDB.read_bytes()).hexdigest()


def test_build_prompt(db: promptdb.PromptDB) -> None:
    step = db.step(12)
    prompt = db.build_prompt(step, "claude", "claude-sonnet-5-5")
    assert prompt.startswith(promptdb.FRAMEWORK_PREAMBLE)
    assert "Assisted-by: claude:claude-sonnet-5-5." in prompt
    assert "<harness>" not in prompt
    assert prompt.endswith(f"Step 12 — Sortable projects table (b0f2118)\n\n{step.body}\n")
    assert "plans/access_db_schema.sql has already been supplied" not in prompt


def test_step_3_says_schema_is_supplied(db: promptdb.PromptDB) -> None:
    prompt = db.build_prompt(db.step(3), "opencode", "lumen/glm-5.3")
    assert "plans/access_db_schema.sql has already been supplied" in prompt
    assert "Assisted-by: opencode:lumen/glm-5.3." in prompt


def test_step_43_is_opt_in(db: promptdb.PromptDB) -> None:
    with pytest.raises(KeyError):
        db.step(43)
    with_43 = promptdb.load(PROMPTDB, include_step_43=True)
    step = with_43.step(43)
    assert len(with_43.steps) == 43
    assert step.phase == 8
    assert step.source_commits == ()
    assert step.heading == "Step 43 — Uncommitted follow-up work"
    assert "deploy/dagster.yaml.example" in step.body
    assert "Caveats" not in step.body


SYNTHETIC = """\
Title
Conventions for every step (from CLAUDE.md):

Commit with trailer Assisted-by: <harness>:<model>.
Repo layout the steps converge on:

Phase 1 — First
Step 1 — One (aaaaaaa)
Do one.

Second paragraph.


Step {second} — Two (bbbbbbb, ccccccc)
Do two.
Caveats
Ignore this.
"""


def test_multi_paragraph_body() -> None:
    db = promptdb.parse(SYNTHETIC.format(second=2), sha256="x")
    assert db.step(1).body == "Do one.\n\nSecond paragraph."
    assert db.step(2).body == "Do two."
    assert db.conventions == (
        "Conventions for every step:\n\n- Commit with trailer Assisted-by: <harness>:<model>."
    )


def test_rejects_gaps_in_numbering() -> None:
    with pytest.raises(promptdb.PromptDBError, match="consecutively"):
        promptdb.parse(SYNTHETIC.format(second=3), sha256="x")


def test_rejects_step_before_phase() -> None:
    text = SYNTHETIC.format(second=2).replace("Phase 1 — First\n", "")
    with pytest.raises(promptdb.PromptDBError, match="before any phase"):
        promptdb.parse(text, sha256="x")


def test_cli_list_and_show() -> None:
    runner = CliRunner()
    listed = runner.invoke(app, ["prompts", "list", "--promptdb", str(PROMPTDB)])
    assert listed.exit_code == 0, listed.output
    assert "42 steps" in listed.output
    assert "Phase 7 — CI and deployment" in listed.output

    shown = runner.invoke(
        app,
        [
            "prompts",
            "show",
            "1",
            "--harness",
            "claude",
            "--model",
            "claude-opus-5-5",
            "--promptdb",
            str(PROMPTDB),
        ],
    )
    assert shown.exit_code == 0, shown.output
    assert "Assisted-by: claude:claude-opus-5-5." in shown.output

    missing = runner.invoke(app, ["prompts", "show", "43", "--promptdb", str(PROMPTDB)])
    assert missing.exit_code != 0
