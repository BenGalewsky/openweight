"""Running an implementation's app in its sandbox, to try it from the host.

How the app starts depends on how far the evaluation has got: until step 16 it is the SvelteKit
dev server in ``web/``; from step 16 on, ``docker-compose.yml`` runs the web image (port 3000) with
Postgres and, from step 19, RustFS (console on 9001). Either way the app has to listen on all
interfaces in the sandbox for its forwarder to reach it (see `sandbox.Sandbox.forward`).
"""

from dataclasses import dataclass, field
from pathlib import Path

from openweight.sandbox import WORKSPACE_MOUNT, Forward

DEV_PORT = 5173
WEB_PORT = 3000
RUSTFS_CONSOLE_PORT = 9001
COMPOSE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
# Installs dependencies on a fresh checkout; the agent's own node_modules are reused otherwise.
DEV_SERVER = (
    "test -d node_modules || npm install; "
    f"exec npm run dev -- --host 0.0.0.0 --port {DEV_PORT} --strictPort"
)


class ServeError(RuntimeError):
    """The workspace has no app to start by default."""


@dataclass(frozen=True)
class Launch:
    """A command to start the app in the sandbox and the ports it serves."""

    command: list[str]
    forwards: list[Forward] = field(default_factory=list)
    workdir: str = WORKSPACE_MOUNT


def default_launch(workspace: Path) -> Launch:
    """How to start the app at the workspace's current step."""
    for directory in ("", "web"):
        if any((workspace / directory / name).is_file() for name in COMPOSE_FILES):
            return Launch(
                ["docker", "compose", "up", "--build"],
                [Forward(WEB_PORT, WEB_PORT), Forward(RUSTFS_CONSOLE_PORT, RUSTFS_CONSOLE_PORT)],
                f"{WORKSPACE_MOUNT}/{directory}".rstrip("/"),
            )
    if (workspace / "web" / "package.json").is_file():
        return Launch(
            ["sh", "-c", DEV_SERVER], [Forward(DEV_PORT, DEV_PORT)], f"{WORKSPACE_MOUNT}/web"
        )
    raise ServeError(f"no web/package.json or compose file in {workspace}; pass a command to run")
