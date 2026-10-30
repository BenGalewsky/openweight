"""Per-evaluation sandbox containers.

Every agent session and check runs in a container, never on the host. Each evaluation gets two
containers on a private network ``ow-<name>``:

- ``ow-<name>-dind``: a ``docker:dind`` daemon of its own, with its storage in the volume
  ``ow-<name>-dind``. It listens on loopback only.
- ``ow-<name>-agent``: the ``openweight-sandbox`` image, sharing the dind container's network
  namespace. The agent's ``docker`` talks to that loopback daemon, and ports that compose publishes
  are on the agent's ``localhost``.

The workspace is mounted read-write at ``/workspace`` and ``harness_config/`` read-only at
``/harness_config``; nothing else from the host is mounted. The containers persist across steps
until `Sandbox.down` removes them with their network and volume.
"""

import os
import re
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from openweight import branches

IMAGE_NAME = "openweight-sandbox"
DOCKERFILE = Path(__file__).resolve().parents[2] / "sandbox" / "Dockerfile"
DOCKER_PORT = 2375

WORKSPACE_MOUNT = "/workspace"
HARNESS_CONFIG_MOUNT = "/harness_config"
LABEL = "openweight.evaluation"
DIND_TIMEOUT_S = 60
# Exit status of `timeout` when the command ran out of time (it is killed 10s later if need be).
TIMEOUT_EXIT = 124

HARNESS_VERSION_ARGS = {"claude": "CLAUDE_VERSION", "opencode": "OPENCODE_VERSION"}


class DockerError(RuntimeError):
    """A docker command failed."""


class SandboxError(RuntimeError):
    """A sandbox is not in the expected state."""


@cache
def versions(dockerfile: Path = DOCKERFILE) -> dict[str, str]:
    """The pinned versions: the Dockerfile's global ``ARG <NAME>_VERSION=<value>`` lines."""
    pins = re.findall(r"^ARG (\w+_VERSION)=(\S+)$", dockerfile.read_text(), re.MULTILINE)
    return dict(pins)


def image_tag() -> str:
    pins = versions()
    return f"{IMAGE_NAME}:claude-{pins['CLAUDE_VERSION']}-opencode-{pins['OPENCODE_VERSION']}"


def dind_image() -> str:
    return f"docker:{versions()['DOCKER_VERSION']}-dind"


def harness_version(harness: str) -> str:
    """The pinned CLI version of ``harness`` in the sandbox image, e.g. ``claude 2.1.312``."""
    return f"{harness} {versions()[HARNESS_VERSION_ARGS[harness]]}"


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run the docker CLI and capture its output, raising `DockerError` on failure if ``check``."""
    try:
        result = subprocess.run(["docker", *args], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        raise DockerError("docker is not installed (or not on PATH)") from None
    if check and result.returncode != 0:
        raise DockerError(f"docker {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result


def check_docker() -> None:
    """Fail with a clear message if the Docker daemon cannot be reached."""
    result = docker("version", "--format", "{{.Server.Version}}", check=False)
    if result.returncode != 0:
        raise DockerError(f"cannot reach the Docker daemon (is it running?):\n{result.stderr}")


def image_exists(tag: str) -> bool:
    return docker("image", "inspect", tag, check=False).returncode == 0


def build_image(context: Path, tag: str | None = None, pull: bool = False) -> None:
    """Build the sandbox image from ``context`` (the repo's ``sandbox/``), streaming its output."""
    tag = tag or image_tag()
    args = ["--pull"] if pull else []
    result = subprocess.run(["docker", "build", "--tag", tag, *args, str(context)], check=False)
    if result.returncode != 0:
        raise DockerError(f"building {tag} failed")


@dataclass(frozen=True)
class Limits:
    """Resource limits per evaluation."""

    agent_cpus: float = 4
    agent_memory: str = "8g"
    dind_cpus: float = 4
    dind_memory: str = "4g"


class Sandbox:
    """The containers, network, and volume of one evaluation."""

    def __init__(
        self,
        name: str,
        workspace: Path,
        harness_config: Path,
        limits: Limits | None = None,
        image: str | None = None,
    ) -> None:
        self.name = name
        self.workspace = workspace
        self.harness_config = harness_config
        self.limits = limits or Limits()
        self.image = image or image_tag()

    @classmethod
    def for_branch(cls, repo: Path, branch: str, limits: Limits | None = None) -> "Sandbox":
        name = branches.workspace_name(branch)
        return cls(name, branches.workspace_dir(repo, name), repo / "harness_config", limits)

    @property
    def network(self) -> str:
        return f"ow-{self.name}"

    @property
    def dind(self) -> str:
        return f"ow-{self.name}-dind"

    @property
    def agent(self) -> str:
        return f"ow-{self.name}-agent"

    @property
    def volume(self) -> str:
        return f"ow-{self.name}-dind"

    def dind_args(self) -> list[str]:
        """``docker run`` arguments for the dind container."""
        return [
            "run", "--detach",
            "--name", self.dind,
            "--label", f"{LABEL}={self.name}",
            "--network", self.network,
            "--privileged",  # required by dind; it reaches only its volume and the network
            f"--cpus={self.limits.dind_cpus}",
            f"--memory={self.limits.dind_memory}",
            "--env", "DOCKER_TLS_CERTDIR=",
            "--volume", f"{self.volume}:/var/lib/docker",
            dind_image(),
            # Naming dockerd keeps the image's entrypoint from adding a 0.0.0.0 listener. Plain
            # TCP is fine on loopback: only the agent shares this network namespace.
            "dockerd",
            "--host=unix:///var/run/docker.sock",
            f"--host=tcp://127.0.0.1:{DOCKER_PORT}",
            "--tls=false",
        ]  # fmt: skip

    def agent_args(self) -> list[str]:
        """``docker run`` arguments for the agent container."""
        return [
            "run", "--detach",
            "--name", self.agent,
            "--label", f"{LABEL}={self.name}",
            "--network", f"container:{self.dind}",
            f"--cpus={self.limits.agent_cpus}",
            f"--memory={self.limits.agent_memory}",
            "--volume", f"{self.workspace.resolve()}:{WORKSPACE_MOUNT}",
            "--volume", f"{self.harness_config.resolve()}:{HARNESS_CONFIG_MOUNT}:ro",
            "--workdir", WORKSPACE_MOUNT,
            self.image,
        ]  # fmt: skip

    def exec_args(
        self,
        command: Sequence[str],
        env_keys: Sequence[str] = (),
        interactive: bool = False,
        workdir: str = WORKSPACE_MOUNT,
    ) -> list[str]:
        """``docker exec`` arguments. Only the names of ``env_keys`` appear in the arguments: the
        values are taken from docker's own environment, so secrets never show in ``ps``."""
        args = ["exec", "--workdir", workdir]
        if interactive:
            args.append("--interactive")
            if os.isatty(0):
                args.append("--tty")
        for key in env_keys:
            args += ["--env", key]
        return [*args, self.agent, *command]

    def state(self, container: str) -> str | None:
        """The container's status (``running``, ``exited``, ...), or None if it does not exist."""
        result = docker("container", "inspect", "--format", "{{.State.Status}}", container,
                        check=False)  # fmt: skip
        return result.stdout.strip() if result.returncode == 0 else None

    def status(self) -> dict[str, str | None]:
        return {"dind": self.state(self.dind), "agent": self.state(self.agent)}

    def is_up(self) -> bool:
        return self.status() == {"dind": "running", "agent": "running"}

    def up(self) -> list[str]:
        """Start the sandbox, creating whatever is missing. Returns what was done.

        Safe to call before every step: running containers are left alone. The agent container is
        recreated if dind was recreated (its network namespace is gone) or the image changed.
        """
        if not (self.workspace / ".git").is_dir():
            raise SandboxError(f"no workspace at {self.workspace} (run `workspace open` first)")
        check_docker()
        if not image_exists(self.image):
            raise SandboxError(f"no image {self.image} (run `openweight sandbox build`)")
        actions = []

        if docker("network", "inspect", self.network, check=False).returncode != 0:
            docker("network", "create", "--label", f"{LABEL}={self.name}", self.network)
            actions.append(f"created network {self.network}")

        dind_state = self.state(self.dind)
        if dind_state is None:
            docker(*self.dind_args())
            actions.append(f"created {self.dind}")
        elif dind_state != "running":
            docker("start", self.dind)
            actions.append(f"started {self.dind}")
        self._wait_for_dind()

        agent_state = self.state(self.agent)
        if agent_state is not None:
            image = docker("container", "inspect", "--format", "{{.Config.Image}}", self.agent)
            stale = image.stdout.strip() != self.image
            if stale or dind_state is None:
                docker("rm", "--force", self.agent)
                actions.append(f"removed {self.agent}" + (" (image changed)" if stale else ""))
                agent_state = None
        if agent_state is None:
            docker(*self.agent_args())
            actions.append(f"created {self.agent}")
        elif dind_state != "running" or agent_state != "running":
            # Restarting rejoins the restarted dind container's network namespace.
            docker("restart", self.agent)
            actions.append(f"restarted {self.agent}")
        return actions

    def _wait_for_dind(self) -> None:
        deadline = time.monotonic() + DIND_TIMEOUT_S
        while True:
            result = docker("exec", self.dind, "docker", "info", check=False)
            if result.returncode == 0:
                return
            if self.state(self.dind) != "running":
                logs = docker("logs", "--tail", "20", self.dind, check=False)
                raise SandboxError(f"{self.dind} exited:\n{logs.stdout}{logs.stderr}")
            if time.monotonic() > deadline:
                raise SandboxError(f"{self.dind} not ready after {DIND_TIMEOUT_S}s")
            time.sleep(0.5)

    def down(self) -> list[str]:
        """Remove the containers, network, and dind volume. Returns what was removed."""
        check_docker()
        removed = []
        for container in (self.agent, self.dind):
            if self.state(container) is not None:
                docker("rm", "--force", "--volumes", container)
                removed.append(container)
        if docker("network", "inspect", self.network, check=False).returncode == 0:
            docker("network", "rm", self.network)
            removed.append(f"network {self.network}")
        if docker("volume", "inspect", self.volume, check=False).returncode == 0:
            docker("volume", "rm", self.volume)
            removed.append(f"volume {self.volume}")
        return removed

    def _require_up(self) -> None:
        if not self.is_up():
            raise SandboxError(f"sandbox {self.name} is not running (run `openweight sandbox up`)")

    def exec(
        self,
        command: Sequence[str],
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        workdir: str = WORKSPACE_MOUNT,
    ) -> subprocess.CompletedProcess[str]:
        """Run ``command`` as the agent user and capture its output. Does not check the exit code.

        With ``timeout`` (seconds) the command runs under ``timeout`` inside the container, so it
        is the command that gets killed, not just ``docker exec``; it then exits `TIMEOUT_EXIT`.
        """
        self._require_up()
        env = dict(env or {})
        if timeout is not None:
            command = ["timeout", "--kill-after=10", f"{timeout:g}", *command]
        return subprocess.run(
            ["docker", *self.exec_args(command, list(env), workdir=workdir)],
            capture_output=True,
            text=True,
            env=os.environ | env,
            check=False,
        )

    def shell(self, command: Sequence[str] = ("bash", "--login")) -> int:
        """Run an interactive command (by default a shell) attached to this terminal."""
        self._require_up()
        return subprocess.run(
            ["docker", *self.exec_args(command, interactive=True)], check=False
        ).returncode


def list_sandboxes() -> dict[str, dict[str, str]]:
    """Every evaluation's containers on this Docker host: ``{name: {container: status}}``."""
    out = docker(
        "ps", "--all", "--filter", f"label={LABEL}",
        "--format", f'{{{{.Label "{LABEL}"}}}}\t{{{{.Names}}}}\t{{{{.State}}}}',
    ).stdout  # fmt: skip
    sandboxes: dict[str, dict[str, str]] = {}
    for line in out.splitlines():
        name, container, state = line.split("\t")
        sandboxes.setdefault(name, {})[container] = state
    return sandboxes
