import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from openweight import branches
from openweight.branches import Evaluation, Workspace, git
from openweight.cli import app

CLAUDE = Evaluation("claude", "claude-sonnet-5-5", "high")
OPENCODE = Evaluation("opencode", "lumen/glm-5.3")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A main repository with the benchmark inputs in place."""
    repo = tmp_path / "main"
    files = {
        "plans/ptd_review_window.md": "# PTD\n",
        "plans/samples-redesign.md": "# Samples\n",
        "db/access_db_schema.sql": "CREATE TABLE t (id int);\n",
        "data/DIVER.CSV": "a,b\n",
        "data/VuSitu.html": "<html></html>\n",
        "src/openweight/cli.py": "# framework code\n",
    }
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    for name in branches.ACCESS_DBS:
        (repo / "db" / name).write_bytes(b"access " + name.encode())
        (repo / "db" / name).chmod(0o600)
    (repo / ".gitignore").write_text("*.accdb\nworkspaces/\n")
    git(repo, "init", "--quiet", "--initial-branch=main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "commit.gpgsign", "false")
    git(repo, "add", "--all")
    git(repo, "commit", "--quiet", "-m", "framework")
    # Untracked files in plans/ and data/ are not inputs.
    (repo / "plans" / "scratch.md").write_text("draft\n")
    (repo / "data" / "Projects.json").write_text("{}\n")
    return repo


def agent_commit(workspace: Workspace, rel: str, text: str, message: str) -> None:
    (workspace.path / rel).parent.mkdir(parents=True, exist_ok=True)
    (workspace.path / rel).write_text(text)
    workspace.git("add", "--all")
    workspace.git("commit", "--quiet", "-m", message)


def message(workspace: Workspace, rev: str = "HEAD") -> str:
    return workspace.git("log", "-1", "--format=%B", rev).strip()


@pytest.mark.parametrize(
    ("evaluation", "slug", "branch", "name"),
    [
        (CLAUDE, "claude-sonnet-5-5-high", "eval/claude/claude-sonnet-5-5-high",
         "claude--claude-sonnet-5-5-high"),
        (OPENCODE, "lumen--glm-5.3", "eval/opencode/lumen--glm-5.3", "opencode--lumen--glm-5.3"),
        (Evaluation("opencode", "Lumen/Org:Model"), "lumen--org--model",
         "eval/opencode/lumen--org--model", "opencode--lumen--org--model"),
    ],
)  # fmt: skip
def test_names(evaluation: Evaluation, slug: str, branch: str, name: str, tmp_path: Path) -> None:
    assert evaluation.slug == slug
    assert evaluation.branch == branch
    assert evaluation.name == name
    assert evaluation.tag(7) == f"{branch}/step-07"
    git(tmp_path, "check-ref-format", f"refs/heads/{branch}")
    git(tmp_path, "check-ref-format", f"refs/tags/{evaluation.tag(42)}")


def test_rejects_unknown_harness() -> None:
    with pytest.raises(ValueError, match="unknown harness"):
        Evaluation("cursor", "x")


def test_parse_trailers() -> None:
    text = "Assisted-by: claude:claude-opus-5-5\nEval-Step: 3\nEval-Step: 4\n"
    assert branches.parse_trailers(text) == {
        "Assisted-by": ["claude:claude-opus-5-5"],
        "Eval-Step": ["3", "4"],
    }


def test_creates_orphan_branch_with_context(repo: Path) -> None:
    ws = branches.open_workspace(repo, CLAUDE)

    assert ws.path == repo / "workspaces" / CLAUDE.name
    assert ws.current_branch() == CLAUDE.branch
    assert ws.git("rev-list", "--count", "HEAD").strip() == "1"
    assert message(ws) == branches.ROOT_COMMIT_SUBJECT
    assert ws.git("ls-tree", "-r", "--name-only", "HEAD").split() == [
        "plans/access_db_schema.sql",
        "plans/ptd_review_window.md",
        "plans/samples-redesign.md",
    ]
    assert ws.tag_sha(0) == ws.head()
    assert ws.next_step() == 1
    # The workspace holds only the eval branch, none of main's history.
    assert ws.git("for-each-ref", "--format=%(refname)", "refs/heads").split() == [
        f"refs/heads/{CLAUDE.branch}"
    ]
    assert ws.git("log", "-1", "--format=%an", "HEAD").strip() == branches.RUNNER_NAME
    assert ws.git("config", "user.name").strip() == branches.AGENT_NAME


def test_places_untracked_inputs(repo: Path) -> None:
    ws = branches.open_workspace(repo, CLAUDE)

    assert (ws.path / "IDOT.accdb").read_bytes() == b"access IDOT.accdb"
    assert (ws.path / "Wetlands_IDOT_BE.accdb").stat().st_mode & 0o777 == 0o644
    assert (ws.path / "data" / "DIVER.CSV").read_text() == "a,b\n"
    assert (ws.path / "data" / "VuSitu.html").exists()
    assert not (ws.path / "data" / "Projects.json").exists()
    assert not (ws.path / "plans" / "scratch.md").exists()
    # Excluded via .git/info/exclude, so the workspace is clean and `git add -A` skips them.
    assert ws.is_clean()
    ws.git("add", "--all")
    assert ws.git("diff", "--cached", "--name-only") == ""
    ws.ensure_ready(1)


def test_inputs_are_copies(repo: Path) -> None:
    ws = branches.open_workspace(repo, CLAUDE)
    (ws.path / "IDOT.accdb").write_bytes(b"modified")
    assert (repo / "db" / "IDOT.accdb").read_bytes() == b"access IDOT.accdb"


def test_fails_fast_on_missing_inputs(repo: Path) -> None:
    (repo / "db" / "Wetlands_IDOT_BE.accdb").unlink()
    with pytest.raises(branches.WorkspaceError, match="Wetlands_IDOT_BE.accdb"):
        branches.open_workspace(repo, CLAUDE)
    assert not (repo / "workspaces").exists()


def test_record_step_amends_agent_commit(repo: Path) -> None:
    ws = branches.open_workspace(repo, CLAUDE)
    agent_commit(ws, "web/a.txt", "a", "feat(web): a\n\nAssisted-by: claude:claude-sonnet-5-5")
    agent_commit(ws, "web/b.txt", "b", "feat(web): b")

    record = ws.record_step(1, run_id="abc123")

    assert record.agent_commits == 2
    assert record.agent_committed
    assert record.tag == CLAUDE.tag(1)
    assert ws.tag_sha(1) == record.sha == ws.head()
    # The trailers go onto the agent's own commit, which keeps its author and its own
    # (missing) Assisted-by trailer.
    assert message(ws) == "feat(web): b\n\nEval-Step: 1\nEval-Run-Id: abc123"
    assert ws.git("log", "-1", "--format=%an", "HEAD").strip() == branches.AGENT_NAME
    assert ws.step_commits() == [branches.StepCommit(record.sha, 1, "abc123")]
    assert ws.next_step() == 2


def test_record_step_commits_leftover_changes(repo: Path) -> None:
    ws = branches.open_workspace(repo, OPENCODE)
    agent_commit(ws, "web/a.txt", "a", "feat(web): a")
    (ws.path / "web" / "a.txt").write_text("changed")
    (ws.path / "web" / "new.txt").write_text("new")

    record = ws.record_step(1)

    assert record.agent_commits == 1
    assert message(ws) == (
        "chore(eval): uncommitted changes from step 1\n\n"
        "Assisted-by: opencode:lumen/glm-5.3\nEval-Step: 1"
    )
    assert message(ws, "HEAD~1") == "feat(web): a"
    assert ws.is_clean()
    assert ws.git("log", "-1", "--format=%an", "HEAD").strip() == branches.RUNNER_NAME


def test_record_step_without_agent_commits(repo: Path) -> None:
    ws = branches.open_workspace(repo, OPENCODE)
    (ws.path / "notes.txt").write_text("uncommitted")
    record = ws.record_step(1)
    assert not record.agent_committed
    assert message(ws).startswith("chore(eval): uncommitted changes from step 1")

    record = ws.record_step(2)
    assert not record.agent_committed
    assert message(ws).startswith("chore(eval): no changes from step 2\n\n")
    assert ws.git("diff", "--name-only", OPENCODE.tag(1), "HEAD") == ""
    assert [c.step for c in ws.step_commits()] == [1, 2]
    assert ws.next_step() == 3
    ws.ensure_ready(3)


def test_record_step_requires_eval_branch(repo: Path) -> None:
    ws = branches.open_workspace(repo, CLAUDE)
    ws.git("checkout", "--quiet", "-b", "feature")
    with pytest.raises(branches.WorkspaceError, match="is on feature"):
        ws.record_step(1)


def test_ensure_ready(repo: Path) -> None:
    ws = branches.open_workspace(repo, CLAUDE)
    with pytest.raises(branches.WorkspaceError, match="next step .* is 1, not 2"):
        ws.ensure_ready(2)
    (ws.path / "x.txt").write_text("x")
    with pytest.raises(branches.WorkspaceError, match="uncommitted"):
        ws.ensure_ready(1)
    ws.git("add", "x.txt")
    ws.git("commit", "--quiet", "-m", "x")
    with pytest.raises(branches.WorkspaceError, match="is not at"):
        ws.ensure_ready(1)


def test_reset_to_discards_later_work(repo: Path) -> None:
    ws = branches.open_workspace(repo, CLAUDE)
    agent_commit(ws, "a.txt", "a", "feat: a")
    ws.record_step(1)
    agent_commit(ws, "b.txt", "b", "feat: b")
    ws.record_step(2)
    agent_commit(ws, "c.txt", "c", "feat: c")
    (ws.path / "untracked.txt").write_text("u")
    (ws.path / ".gitignore").write_text("node_modules/\n")
    ws.git("add", ".gitignore")
    (ws.path / "node_modules").mkdir()
    (ws.path / "node_modules" / "pkg.js").write_text("")

    ws.reset_to(1)

    assert ws.head() == ws.tag_sha(1)
    assert ws.tag_sha(2) is None
    assert ws.next_step() == 2
    assert not (ws.path / "b.txt").exists()
    assert not (ws.path / "untracked.txt").exists()
    assert (ws.path / "IDOT.accdb").exists()
    ws.ensure_ready(2)


def test_sync_to_main_and_restore(repo: Path) -> None:
    ws = branches.open_workspace(repo, CLAUDE)
    agent_commit(ws, "a.txt", "a", "feat: a")
    ws.record_step(1, run_id="r1")
    agent_commit(ws, "b.txt", "b", "feat: b")
    ws.record_step(2, run_id="r2")
    branches.sync_to_main(repo, ws)

    assert branches.eval_branches(repo) == [CLAUDE.branch]
    assert branches.next_step(repo, CLAUDE.branch) == 3
    tags = git(repo, "tag", "--list", f"{CLAUDE.branch}/*").split()
    assert tags == [CLAUDE.tag(0), CLAUDE.tag(1), CLAUDE.tag(2)]
    assert git(repo, "rev-parse", "HEAD") == git(repo, "rev-parse", "main")

    # A reset in the workspace (e.g. a step rerun) is mirrored, pruning the stale tag.
    ws.reset_to(1)
    branches.sync_to_main(repo, ws)
    assert git(repo, "tag", "--list", f"{CLAUDE.branch}/*").split() == tags[:2]
    assert branches.next_step(repo, CLAUDE.branch) == 2

    # Losing the workspace is recoverable from the main repository.
    shutil.rmtree(ws.path)
    restored = branches.open_workspace(repo, CLAUDE)
    assert restored.head() == git(repo, "rev-parse", CLAUDE.branch).strip()
    assert restored.tag_sha(1) == restored.head()
    assert restored.current_branch() == CLAUDE.branch
    assert (restored.path / "IDOT.accdb").exists()
    assert restored.is_clean()
    restored.ensure_ready(2)


def test_reopen_existing_workspace(repo: Path) -> None:
    ws = branches.open_workspace(repo, CLAUDE)
    agent_commit(ws, "a.txt", "a", "feat: a")
    (ws.path / "IDOT.accdb").unlink()

    again = branches.open_workspace(repo, CLAUDE)

    assert again.head() == ws.head()
    assert (again.path / "IDOT.accdb").exists()
    exclude = (ws.path / ".git" / "info" / "exclude").read_text()
    assert exclude.count("/IDOT.accdb") == 1


def test_workspace_without_git_dir_is_refused(repo: Path) -> None:
    path = repo / "workspaces" / CLAUDE.name
    path.mkdir(parents=True)
    with pytest.raises(branches.WorkspaceError, match="no .git directory"):
        branches.open_workspace(repo, CLAUDE)


def test_cli_open_and_list(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(repo)
    runner = CliRunner()

    listed = runner.invoke(app, ["workspace", "list"])
    assert listed.exit_code == 0, listed.output
    assert "no eval branches" in listed.output

    opened = runner.invoke(app, ["workspace", "open", "--harness", "opencode", "--model",
                                 "lumen/glm-5.3"])  # fmt: skip
    assert opened.exit_code == 0, opened.output
    assert "workspaces/opencode--lumen--glm-5.3" in opened.output
    assert "next step  1" in opened.output

    listed = runner.invoke(app, ["workspace", "list"])
    assert "eval/opencode/lumen--glm-5.3  next step 1" in listed.output

    bad = runner.invoke(app, ["workspace", "open", "--harness", "cursor", "--model", "x"])
    assert bad.exit_code != 0


@pytest.mark.parametrize("evaluation", [CLAUDE, OPENCODE])
def test_workspace_name_from_branch(evaluation: Evaluation) -> None:
    assert branches.workspace_name(evaluation.branch) == evaluation.name


@pytest.mark.parametrize(
    "branch", ["main", "eval/cursor/x", "eval/claude", "eval/claude/", "eval/claude/a/b"]
)
def test_workspace_name_rejects_other_branches(branch: str) -> None:
    with pytest.raises(ValueError, match="not an eval branch"):
        branches.workspace_name(branch)
