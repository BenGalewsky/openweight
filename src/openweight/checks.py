"""Objective checks run in the evaluation's sandbox after each step.

Each check is a shell command run in a directory of the workspace, from the step that introduces
its tooling on; before that step it is skipped, not failed. A check whose prerequisite (``needs``)
failed is failed without running.

Checks run on a copy of the workspace in the agent container (``/tmp/openweight-checks``), never in
``/workspace`` itself, so they cannot change the agent's work or environment: a failed ``npm ci``
in place would leave the next session without ``node_modules``. The copy is the working tree as
the agent left it, uncommitted changes and ignored files (``.env``, ``data/``) included, minus
``.git``, the Access databases, and installed dependencies (``node_modules``, ``.venv``), which the
install checks recreate from the lock files.

Each check that ran writes ``<name>.log`` to the log directory, next to a ``checks.json``
summary, and becomes a 0/1 ``check.<name>`` metric.
"""

import json
import re
import shutil
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from openweight.branches import AgentCommit
from openweight.sandbox import TIMEOUT_EXIT, WORKSPACE_MOUNT, Sandbox

PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"

CHECKOUT = "/tmp/openweight-checks"
COPY_EXCLUDES = (".git", "node_modules", ".venv", "*.accdb")
# The seed check's throwaway Postgres, on a port that cannot clash with the agent's compose stack.
SEED_POSTGRES_IMAGE = "postgres:16-alpine"
SEED_POSTGRES_PORT = 55432
SEED_POSTGRES_NAME = "ow-check-postgres"
# How much of a failing check's log the retry prompt quotes.
LOG_TAIL_LINES = 60

SEED_SCRIPT = f"""\
docker rm --force {SEED_POSTGRES_NAME} >/dev/null 2>&1
trap 'docker rm --force {SEED_POSTGRES_NAME} >/dev/null 2>&1' EXIT
docker run --detach --quiet --name {SEED_POSTGRES_NAME} \\
  --env POSTGRES_USER=idot --env POSTGRES_PASSWORD=idot --env POSTGRES_DB=idot \\
  --publish 127.0.0.1:{SEED_POSTGRES_PORT}:5432 {SEED_POSTGRES_IMAGE} >/dev/null || exit 1
for _ in $(seq 60); do
  pg_isready --quiet --host=localhost --port={SEED_POSTGRES_PORT} && break
  sleep 1
done
pg_isready --host=localhost --port={SEED_POSTGRES_PORT} || exit 1
export DATABASE_URL=postgresql://idot:idot@localhost:{SEED_POSTGRES_PORT}/idot
npm run db:push && npm run db:seed
"""

_EXCLUDES = " ".join(f"--exclude='{pattern}'" for pattern in COPY_EXCLUDES)
COPY_SCRIPT = f"""\
rm -rf {CHECKOUT} && mkdir -p {CHECKOUT} &&
tar --create {_EXCLUDES} . | tar --extract --directory={CHECKOUT}
"""

CONVENTIONAL_SUBJECT = re.compile(r"^[a-z]+(\([^()]+\))?!?: \S")


class CheckError(RuntimeError):
    """The checks could not be set up (not a failure of the work being checked)."""


@dataclass(frozen=True)
class Check:
    name: str
    title: str
    command: str
    # Workspace-relative directory the command runs in; it must exist.
    cwd: str = "."
    from_step: int = 1
    until_step: int | None = None
    # Checks that must pass for this one to be worth running.
    needs: tuple[str, ...] = ()
    timeout_s: float = 10 * 60
    # Left out unless asked for (slow).
    opt_in: bool = False

    def applies(self, step: int) -> bool:
        return self.from_step <= step and (self.until_step is None or step <= self.until_step)

    @property
    def metric(self) -> str:
        return f"check.{self.name}"

    @property
    def workdir(self) -> str:
        return CHECKOUT if self.cwd == "." else f"{CHECKOUT}/{self.cwd}"

    def describe(self) -> str:
        where = "" if self.cwd == "." else f" in {self.cwd}/"
        command = self.command if "\n" not in self.command else self.command.splitlines()[-1]
        return f"`{command}`{where}"


# fmt: off
CHECKS = (
    Check("web_install", "web install", "npm ci", "web"),
    Check("web_typecheck", "web typecheck", "npm run check", "web", needs=("web_install",)),
    Check("web_lint", "web lint", "npm run lint", "web", needs=("web_install",)),
    Check("web_build", "web build", "npm run build", "web", needs=("web_install",)),
    Check(
        "db_seed", "db push + seed", SEED_SCRIPT, "web", from_step=15, needs=("web_install",),
        timeout_s=20 * 60,
    ),
    Check("compose_config", "compose config", "docker compose config --quiet", from_step=16),
    Check("dagster_install", "dagster install", "uv sync", "isgs_dagster", from_step=21),
    Check(
        "dagster_defs", "dagster defs load", "uv run dg check defs", "isgs_dagster",
        from_step=21, needs=("dagster_install",),
    ),
    Check(
        "dagster_tests", "dagster tests", "uv run pytest", "isgs_dagster", from_step=23,
        needs=("dagster_install",), timeout_s=20 * 60,
    ),
    # Step 14's Dockerfile has no named stages yet; step 16 splits it into builder, seeder, runtime.
    Check(
        "docker_build", "docker build", "docker build .", from_step=14, until_step=15,
        timeout_s=30 * 60, opt_in=True,
    ),
    Check(
        "docker_build", "docker build", "docker build --target runtime .", from_step=16,
        timeout_s=30 * 60, opt_in=True,
    ),
)
# fmt: on


def default_checks(docker_build: bool = False) -> list[Check]:
    """The standard checks, with the opt-in ones if asked for."""
    return [check for check in CHECKS if docker_build or not check.opt_in]


@dataclass(frozen=True)
class CheckResult:
    check: Check
    status: str
    log: str = ""
    exit_code: int | None = None
    duration_s: float = 0

    @property
    def ran(self) -> bool:
        return self.status != SKIPPED

    def summary(self) -> dict[str, object]:
        return {
            "name": self.check.name,
            "title": self.check.title,
            "command": self.check.command,
            "cwd": self.check.cwd,
            "status": self.status,
            "exit_code": self.exit_code,
            "duration_s": round(self.duration_s, 1),
        }


def run_checks(
    box: Sandbox,
    workspace: Path,
    step: int,
    checks: Sequence[Check],
    log_dir: Path,
    log: Callable[[str], None] = print,
) -> list[CheckResult]:
    """Run the ``checks`` that apply to ``step`` on a copy of ``workspace`` in the sandbox, in
    order, writing their logs to ``log_dir`` (replaced if it exists)."""
    if log_dir.exists():
        shutil.rmtree(log_dir)
    log_dir.mkdir(parents=True)
    applicable = {check.name for check in checks if check.applies(step)}
    if applicable:
        _copy_workspace(box)
    try:
        results = _run_all(box, workspace, step, checks, applicable, log_dir, log)
    finally:
        if applicable:
            box.exec(["rm", "-rf", CHECKOUT])
    summary = [result.summary() for result in results]
    (log_dir / "checks.json").write_text(json.dumps(summary, indent=2) + "\n")
    return results


def _run_all(
    box: Sandbox,
    workspace: Path,
    step: int,
    checks: Sequence[Check],
    applicable: set[str],
    log_dir: Path,
    log: Callable[[str], None],
) -> list[CheckResult]:
    results: list[CheckResult] = []
    status: dict[str, str] = {}
    for check in checks:
        if not check.applies(step):
            if check.name in applicable or check.name in status:  # another variant covers it
                continue
            result = CheckResult(check, SKIPPED)
        elif blocked := [need for need in check.needs if status.get(need) == FAILED]:
            result = CheckResult(check, FAILED, f"not run: {', '.join(blocked)} failed\n")
        elif not (workspace / check.cwd).is_dir():
            result = CheckResult(check, FAILED, f"not run: {check.cwd}/ does not exist\n")
        else:
            log(f"step {step}: check {check.title}")
            result = _run(box, check)
        status[check.name] = result.status
        results.append(result)
        if result.ran:
            (log_dir / f"{check.name}.log").write_text(result.log)
    return results


def _copy_workspace(box: Sandbox) -> None:
    proc = box.exec(["bash", "-c", COPY_SCRIPT], workdir=WORKSPACE_MOUNT)
    if proc.returncode != 0:
        raise CheckError(f"copying the workspace to {CHECKOUT} failed:\n{proc.stderr}")


def _run(box: Sandbox, check: Check) -> CheckResult:
    # No other configuration: the copy has the agent's own .env files, and nothing else is set
    # (PromptDB's Postgres DATABASE_URL would be a file path to the SQLite-era app).
    env = {"CI": "true"}
    started = time.monotonic()
    proc = box.exec(
        ["bash", "-c", f"exec 2>&1\n{check.command}"],
        env=env,
        timeout=check.timeout_s,
        workdir=check.workdir,
    )
    duration_s = time.monotonic() - started
    output = proc.stdout or ""
    if output and not output.endswith("\n"):
        output += "\n"
    if proc.returncode == TIMEOUT_EXIT:
        footer = f"timed out after {check.timeout_s / 60:g} min"
    else:
        footer = f"exit {proc.returncode} after {duration_s:.1f}s"
    text = f"$ {check.command.strip()}\n(in {check.workdir})\n\n{output}\n{footer}\n"
    status = PASSED if proc.returncode == 0 else FAILED
    return CheckResult(check, status, text, proc.returncode, duration_s)


def metrics(results: Sequence[CheckResult]) -> dict[str, float]:
    """``check.<name>`` (1 passed, 0 failed) for each check that ran, and the counts."""
    if not results:
        return {}
    ran = [result for result in results if result.ran]
    values = {result.check.metric: float(result.status == PASSED) for result in ran}
    passed = sum(result.status == PASSED for result in ran)
    duration_s = sum(result.duration_s for result in ran)
    counts = {"checks_passed": passed, "checks_failed": len(ran) - passed}
    return values | counts | {"checks_duration_s": duration_s}


def new_failures(
    results: Sequence[CheckResult], previous: Mapping[str, float]
) -> list[CheckResult]:
    """The failed checks that did not already fail after the previous step (whose check metrics
    are ``previous``): a check broken since an earlier step does not fail every later one."""
    return [r for r in results if r.status == FAILED and previous.get(r.check.metric) != 0]


def failure_reason(failures: Sequence[CheckResult]) -> str:
    """What the failing checks reported, for the retry prompt."""
    parts = [
        "The session finished, but the work fails these checks, which run in this same "
        "environment after every step:"
    ]
    for result in failures:
        tail = "\n".join(result.log.strip().splitlines()[-LOG_TAIL_LINES:])
        parts.append(f"### {result.check.title}: {result.check.describe()}\n\n```\n{tail}\n```")
    return "\n\n".join(parts)


def commit_metrics(commits: Sequence[AgentCommit], assisted_by: str) -> dict[str, float]:
    """The share of the agent's commits with a conventional-commit subject and with the
    ``Assisted-by`` trailer PromptDB's conventions ask for (none if it made no commits)."""
    if not commits:
        return {}
    conventional = sum(bool(CONVENTIONAL_SUBJECT.match(c.subject)) for c in commits)
    trailer = sum(assisted_by in c.assisted_by for c in commits)
    return {
        "commits_conventional": conventional / len(commits),
        "commits_assisted_by": trailer / len(commits),
    }
