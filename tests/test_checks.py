import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from openweight import branches, checks
from openweight.branches import AgentCommit, Evaluation, git
from openweight.checks import FAILED, PASSED, SKIPPED, Check, CheckResult
from openweight.sandbox import TIMEOUT_EXIT

EVALUATION = Evaluation("opencode", "lumen/glm-5.3")


class FakeBox:
    """Exits with the code given per check command; records what ran where, and the commands that
    set up and remove the checks' copy of the workspace."""

    def __init__(self, exits: dict[str, int] | None = None, copy_exit: int = 0) -> None:
        self.exits = exits or {}
        self.copy_exit = copy_exit
        self.calls: list[tuple[str, str, dict[str, str], float | None]] = []
        self.setup: list[list[str]] = []

    def exec(
        self,
        command: Sequence[str],
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        workdir: str = "/workspace",
    ) -> subprocess.CompletedProcess[str]:
        if not command[-1].startswith("exec 2>&1\n"):
            self.setup.append(list(command))
            code = self.copy_exit if command[0] == "bash" else 0
            return subprocess.CompletedProcess(command, code, "", "tar: boom" if code else "")
        script = command[-1].removeprefix("exec 2>&1\n")
        self.calls.append((script, workdir, dict(env or {}), timeout))
        code = self.exits.get(script, 0)
        return subprocess.CompletedProcess(command, code, f"ran {script}", None)


def run(box: FakeBox, tmp_path: Path, step: int, check_list: Sequence[Check]) -> list[CheckResult]:
    (tmp_path / "ws" / "web").mkdir(parents=True, exist_ok=True)
    return checks.run_checks(
        box, tmp_path / "ws", step, check_list, tmp_path / "logs", log=lambda _: None
    )  # type: ignore[arg-type]


def test_checks_are_gated_by_step(tmp_path: Path) -> None:
    box = FakeBox()
    results = run(box, tmp_path, 15, checks.default_checks())
    status = {r.check.name: r.status for r in results}
    assert status == {
        "web_install": PASSED,
        "web_typecheck": PASSED,
        "web_lint": PASSED,
        "web_build": PASSED,
        "db_seed": PASSED,
        "compose_config": SKIPPED,
        "dagster_install": SKIPPED,
        "dagster_defs": SKIPPED,
        "dagster_tests": SKIPPED,
    }
    script, workdir, env, timeout = box.calls[0]
    assert (script, workdir, timeout) == ("npm ci", f"{checks.CHECKOUT}/web", 600)
    assert env == {"CI": "true"}
    seed = box.calls[-1][0]
    assert f"localhost:{checks.SEED_POSTGRES_PORT}/idot" in seed
    assert seed.endswith("npm run db:push && npm run db:seed\n")


@pytest.mark.parametrize(
    ("step", "command"),
    [(13, None), (14, "docker build ."), (16, "docker build --target runtime .")],
)
def test_docker_build_variants(tmp_path: Path, step: int, command: str | None) -> None:
    assert not any(c.opt_in for c in checks.default_checks())
    builds = [c for c in checks.default_checks(docker_build=True) if c.name == "docker_build"]
    box = FakeBox()
    [result] = run(box, tmp_path, step, builds)
    if command is None:
        assert result.status == SKIPPED and box.calls == []
    else:
        assert result.status == PASSED and box.calls[0][0] == command


def test_failures_block_dependent_checks(tmp_path: Path) -> None:
    box = FakeBox({"npm ci": 1})
    check_list = [
        Check("install", "install", "npm ci", "web"),
        Check("build", "build", "npm run build", "web", needs=("install",)),
        Check("py", "py", "uv sync", "isgs_dagster"),
        Check("compose", "compose", "docker compose config"),
    ]
    results = run(box, tmp_path, 1, check_list)
    assert [r.status for r in results] == [FAILED, FAILED, FAILED, PASSED]
    assert [call[0] for call in box.calls] == ["npm ci", "docker compose config"]

    logs = tmp_path / "logs"
    assert (logs / "build.log").read_text() == "not run: install failed\n"
    assert (logs / "py.log").read_text() == "not run: isgs_dagster/ does not exist\n"
    install = (logs / "install.log").read_text()
    assert install.startswith(f"$ npm ci\n(in {checks.CHECKOUT}/web)\n\nran npm ci\n")
    assert "\nexit 1 after " in install
    summary = json.loads((logs / "checks.json").read_text())
    assert [(s["name"], s["status"], s["exit_code"]) for s in summary] == [
        ("install", FAILED, 1), ("build", FAILED, None), ("py", FAILED, None),
        ("compose", PASSED, 0),
    ]  # fmt: skip


def test_timeout_is_reported(tmp_path: Path) -> None:
    box = FakeBox({"sleep 9": TIMEOUT_EXIT})
    [result] = run(box, tmp_path, 1, [Check("slow", "slow", "sleep 9", timeout_s=90)])
    assert result.status == FAILED
    assert result.log.endswith("timed out after 1.5 min\n")
    assert box.calls[0][3] == 90


def test_log_dir_is_replaced(tmp_path: Path) -> None:
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "stale.log").write_text("old\n")
    run(FakeBox(), tmp_path, 1, [Check("a", "a", "true")])
    assert sorted(p.name for p in (tmp_path / "logs").iterdir()) == ["a.log", "checks.json"]


def results(**statuses: str) -> list[CheckResult]:
    return [
        CheckResult(Check(name, name.upper(), name), status) for name, status in statuses.items()
    ]


def test_metrics() -> None:
    found = checks.metrics(results(a=PASSED, b=FAILED, c=SKIPPED))
    assert found == {
        "check.a": 1,
        "check.b": 0,
        "checks_passed": 1,
        "checks_failed": 1,
        "checks_duration_s": 0,
    }
    assert checks.metrics([]) == {}


def test_new_failures_ignore_checks_already_failing() -> None:
    now = results(a=FAILED, b=FAILED, c=FAILED, d=PASSED)
    previous = {"check.a": 0, "check.b": 1}  # c was skipped (or there is no previous step)
    assert [r.check.name for r in checks.new_failures(now, previous)] == ["b", "c"]


def test_failure_reason_quotes_the_log_tail() -> None:
    log = "".join(f"line {n}\n" for n in range(100))
    failure = CheckResult(Check("build", "web build", "npm run build", "web"), FAILED, log)
    reason = checks.failure_reason([failure])
    assert "### web build: `npm run build` in web/\n\n```\nline 40\n" in reason
    assert "line 39\n" not in reason and reason.endswith("line 99\n```")


def test_commit_metrics() -> None:
    commits = [
        AgentCommit("feat(web): add page", ["opencode:lumen/glm-5.3"]),
        AgentCommit("Add page", ["opencode:lumen/glm-5.3"]),
        AgentCommit("fix!: drop column", []),
        AgentCommit("chore: tidy", ["claude:claude-opus-5-5"]),
    ]
    found = checks.commit_metrics(commits, EVALUATION.assisted_by)
    assert found == {"commits_conventional": 0.75, "commits_assisted_by": 0.5}
    assert checks.commit_metrics([], EVALUATION.assisted_by) == {}


def test_agent_commits_leave_out_the_runners(repo: Path) -> None:
    workspace = branches.open_workspace(repo, EVALUATION)
    for n, trailer in enumerate(["", "\n\nAssisted-by: opencode:lumen/glm-5.3"]):
        (workspace.path / f"f{n}.txt").write_text("x\n")
        workspace.git("add", "--all")
        workspace.git("commit", "--quiet", "-m", f"feat: change {n}{trailer}")
    (workspace.path / "left.txt").write_text("over\n")  # committed by the runner
    workspace.record_step(1, "run-1")
    assert workspace.agent_commits(1) == [
        AgentCommit("feat: change 0", []),
        AgentCommit("feat: change 1", ["opencode:lumen/glm-5.3"]),
    ]
    assert git(workspace.path, "log", "-1", "--format=%an").strip() == branches.RUNNER_NAME


def test_checks_run_on_a_copy_of_the_workspace(tmp_path: Path) -> None:
    box = FakeBox()
    run(box, tmp_path, 1, [Check("a", "a", "true"), Check("b", "b", "true", "web")])
    copy, remove = box.setup
    assert copy[:2] == ["bash", "-c"] and copy[2] == checks.COPY_SCRIPT
    assert "--exclude='node_modules'" in copy[2] and "--exclude='*.accdb'" in copy[2]
    assert remove == ["rm", "-rf", checks.CHECKOUT]
    assert [call[1] for call in box.calls] == [checks.CHECKOUT, f"{checks.CHECKOUT}/web"]


def test_no_copy_when_nothing_applies(tmp_path: Path) -> None:
    box = FakeBox()
    [result] = run(box, tmp_path, 1, [Check("later", "later", "true", from_step=5)])
    assert result.status == SKIPPED
    assert box.setup == [] and box.calls == []


def test_failed_copy_is_an_error_not_a_failed_check(tmp_path: Path) -> None:
    box = FakeBox(copy_exit=1)
    with pytest.raises(checks.CheckError, match="tar: boom"):
        run(box, tmp_path, 1, [Check("a", "a", "true")])
    assert box.calls == []


@pytest.mark.docker
def test_copy_script_leaves_out_dependencies(tmp_path: Path) -> None:
    """The copy script, run for real in a throwaway container."""
    work = tmp_path / "work"
    for rel in ["web/src/app.ts", "web/node_modules/x/index.js", ".git/HEAD", "data/Projects.json",
                "web/.env", "IDOT.accdb", "isgs_dagster/.venv/bin/python"]:  # fmt: skip
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_text("x\n")
    script = checks.COPY_SCRIPT + f"cd {checks.CHECKOUT} && find . -type f | sort"
    result = subprocess.run(
        ["docker", "run", "--rm", "--volume", f"{work}:/workspace:ro", "--workdir", "/workspace",
         "debian:trixie-slim", "bash", "-c", script],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    assert result.stdout.split() == ["./data/Projects.json", "./web/.env", "./web/src/app.ts"]
