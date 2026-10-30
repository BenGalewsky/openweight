import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from openweight import sandbox
from openweight.branches import git
from openweight.cli import app
from openweight.harnesses.claude import MLFLOW_PLUGIN_DIR
from openweight.sandbox import Sandbox

COMPOSE = """\
services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: idot
      POSTGRES_PASSWORD: idot
      POSTGRES_DB: idot
    ports:
      - "5432:5432"
    healthcheck:
      test: ["CMD", "pg_isready", "-U", "idot"]
      interval: 1s
      retries: 30
"""


def test_versions_come_from_dockerfile() -> None:
    pins = sandbox.versions()
    assert {"DOCKER_VERSION", "CLAUDE_VERSION", "OPENCODE_VERSION", "MLFLOW_VERSION"} <= set(pins)
    assert sandbox.image_tag() == (
        f"openweight-sandbox:claude-{pins['CLAUDE_VERSION']}-opencode-{pins['OPENCODE_VERSION']}"
    )
    assert sandbox.dind_image() == f"docker:{pins['DOCKER_VERSION']}-dind"
    assert sandbox.harness_version("opencode") == f"opencode {pins['OPENCODE_VERSION']}"


def test_names_from_branch(tmp_path: Path) -> None:
    box = Sandbox.for_branch(tmp_path, "eval/opencode/lumen--glm-5.3")
    assert box.name == "opencode--lumen--glm-5.3"
    assert box.workspace == tmp_path / "workspaces" / "opencode--lumen--glm-5.3"
    assert box.harness_config == tmp_path / "harness_config"
    assert box.network == "ow-opencode--lumen--glm-5.3"
    assert box.dind == "ow-opencode--lumen--glm-5.3-dind"
    assert box.agent == "ow-opencode--lumen--glm-5.3-agent"
    assert box.volume == "ow-opencode--lumen--glm-5.3-dind"


def test_container_args(tmp_path: Path) -> None:
    box = Sandbox("x", tmp_path / "ws", tmp_path / "hc", sandbox.Limits(agent_cpus=2))
    dind = box.dind_args()
    assert "--privileged" in dind
    assert dind[dind.index("--network") + 1] == "ow-x"
    assert f"--host=tcp://127.0.0.1:{sandbox.DOCKER_PORT}" in dind

    agent = box.agent_args()
    assert "--privileged" not in agent
    assert agent[agent.index("--network") + 1] == "container:ow-x-dind"
    assert "--cpus=2" in agent
    assert volumes(agent) == [f"{tmp_path}/ws:/workspace", f"{tmp_path}/hc:/harness_config:ro"]
    assert volumes(dind) == ["ow-x-dind:/var/lib/docker"]


def volumes(args: list[str]) -> list[str]:
    return [args[i + 1] for i, arg in enumerate(args) if arg == "--volume"]


def test_exec_args_carry_env_names_only(tmp_path: Path) -> None:
    box = Sandbox("x", tmp_path, tmp_path)
    args = box.exec_args(["claude", "-p", "hi"], ["CLAUDE_CODE_OAUTH_TOKEN"])
    assert args == [
        "exec", "--workdir", "/workspace", "--env", "CLAUDE_CODE_OAUTH_TOKEN",
        "ow-x-agent", "claude", "-p", "hi",
    ]  # fmt: skip


def test_up_requires_workspace(tmp_path: Path) -> None:
    with pytest.raises(sandbox.SandboxError, match="no workspace"):
        Sandbox("x", tmp_path / "missing", tmp_path).up()


@pytest.fixture(scope="module")
def live(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Path, str, Sandbox]]:
    """A running sandbox for a throwaway eval branch in a scratch main repository."""
    sandbox.check_docker()
    if not sandbox.image_exists(sandbox.image_tag()):
        pytest.fail(f"no image {sandbox.image_tag()} (run `openweight sandbox build`)")
    repo = tmp_path_factory.mktemp("main")
    git(repo, "init", "--quiet")
    (repo / "harness_config").mkdir()
    (repo / "harness_config" / "mcp.json").write_text("{}\n")
    branch = f"eval/opencode/test-{uuid.uuid4().hex[:8]}"
    box = Sandbox.for_branch(repo, branch)
    box.workspace.mkdir(parents=True)
    git(box.workspace, "init", "--quiet")
    (box.workspace / "compose.yaml").write_text(COMPOSE)
    try:
        yield repo, branch, box
    finally:
        box.down()


def agent_sh(box: Sandbox, script: str, **kwargs: object) -> str:
    result = box.exec(["sh", "-c", script], **kwargs)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.mark.docker
def test_cli_up_list_and_idempotent(
    live: tuple[Path, str, Sandbox], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, branch, box = live
    monkeypatch.chdir(repo)
    runner = CliRunner()

    result = runner.invoke(app, ["sandbox", "up", branch])
    assert result.exit_code == 0, result.output
    assert f"created {box.dind}" in result.output
    assert f"created {box.agent}" in result.output
    assert box.is_up()

    result = runner.invoke(app, ["sandbox", "up", branch])
    assert result.exit_code == 0, result.output
    assert "already running" in result.output

    result = runner.invoke(app, ["sandbox", "list"])
    assert f"{box.name}  {box.agent} running  {box.dind} running" in result.output

    result = runner.invoke(app, ["sandbox", "shell", branch, "sh", "-c", "exit 3"])
    assert result.exit_code == 3, result.output


@pytest.mark.docker
def test_agent_environment(live: tuple[Path, str, Sandbox]) -> None:
    _, _, box = live
    box.up()
    pins = sandbox.versions()
    assert agent_sh(box, "whoami") == "agent"
    assert agent_sh(box, "pwd") == "/workspace"
    assert pins["CLAUDE_VERSION"] in agent_sh(box, "claude --version")
    assert pins["OPENCODE_VERSION"] in agent_sh(box, "opencode --version")
    assert pins["MLFLOW_VERSION"] in agent_sh(box, "mlflow --version")
    assert agent_sh(box, "uv run --no-project python --version").startswith("Python 3.13")
    agent_sh(box, "node --version && mdb-tables --version && psql --version && git --version")
    agent_sh(box, "docker compose version && docker buildx version")
    # The Claude Code tracing plugin, loaded with --plugin-dir.
    agent_sh(box, f"test -f {MLFLOW_PLUGIN_DIR}/.claude-plugin/plugin.json")
    agent_sh(box, f"grep -q stop.cjs {MLFLOW_PLUGIN_DIR}/hooks/hooks.json")

    # The workspace is writable and owned by the agent; harness_config is read-only.
    agent_sh(box, "touch /workspace/from-agent && git status --short")
    assert (box.workspace / "from-agent").exists()
    assert box.exec(["touch", "/harness_config/x"]).returncode != 0
    assert not (box.harness_config / "x").exists()
    # Nothing of the host's Docker is reachable.
    agent_sh(box, "test ! -e /var/run/docker.sock")


@pytest.mark.docker
def test_env_and_timeout(live: tuple[Path, str, Sandbox]) -> None:
    _, _, box = live
    box.up()
    assert agent_sh(box, 'echo "$OW_SECRET"', env={"OW_SECRET": "s3cret"}) == "s3cret"
    assert agent_sh(box, 'echo "${OW_SECRET:-unset}"') == "unset"
    assert box.exec(["sleep", "30"], timeout=1).returncode == sandbox.TIMEOUT_EXIT
    assert box.exec(["pgrep", "--full", "sleep 30"]).returncode == 1  # nothing left running


@pytest.mark.docker
def test_exec_streams_stdout_to_file(live: tuple[Path, str, Sandbox], tmp_path: Path) -> None:
    _, _, box = live
    box.up()
    out = tmp_path / "events.jsonl"
    with out.open("w") as f:
        result = box.exec(["sh", "-c", "echo one; echo two >&2"], stdout=f)
    assert result.returncode == 0
    assert result.stdout is None
    assert result.stderr == "two\n"
    assert out.read_text() == "one\n"


@pytest.mark.docker
def test_compose_postgres_on_localhost(live: tuple[Path, str, Sandbox]) -> None:
    _, _, box = live
    box.up()
    # The daemon is the evaluation's own: it starts with no containers.
    assert agent_sh(box, "docker ps --quiet") == ""
    agent_sh(box, "docker compose up --detach --wait --quiet-pull", timeout=300)
    url = "postgresql://idot:idot@localhost:5432/idot"
    assert agent_sh(box, f"psql {url} -tAc 'select 1'") == "1"


@pytest.mark.docker
def test_up_after_dind_stops(live: tuple[Path, str, Sandbox]) -> None:
    _, _, box = live
    box.up()
    sandbox.docker("stop", box.dind)
    assert not box.is_up()
    actions = box.up()
    assert actions == [f"started {box.dind}", f"restarted {box.agent}"]
    version = agent_sh(box, "docker info --format '{{.ServerVersion}}'")
    assert version == sandbox.versions()["DOCKER_VERSION"]


@pytest.mark.docker
def test_cli_down(live: tuple[Path, str, Sandbox], monkeypatch: pytest.MonkeyPatch) -> None:
    repo, branch, box = live
    monkeypatch.chdir(repo)
    box.up()
    result = CliRunner().invoke(app, ["sandbox", "down", branch])
    assert result.exit_code == 0, result.output
    assert f"removed volume {box.volume}" in result.output
    assert box.status() == {"dind": None, "agent": None}
    assert box.name not in sandbox.list_sandboxes()
    assert (box.workspace / "compose.yaml").exists()
    result = CliRunner().invoke(app, ["sandbox", "down", branch])
    assert f"no sandbox {box.name}" in result.output
