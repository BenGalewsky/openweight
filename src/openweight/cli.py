"""The `openweight` command line."""

from pathlib import Path
from typing import Annotated

import typer

from openweight import branches, checks, harnesses, promptdb, runner, sandbox, serve, tracking

app = typer.Typer(no_args_is_help=True, help="Evaluate coding harnesses and models on PromptDB.")
prompts_app = typer.Typer(no_args_is_help=True, help="Inspect the parsed benchmark prompts.")
app.add_typer(prompts_app, name="prompts")
workspace_app = typer.Typer(no_args_is_help=True, help="Manage evaluation branches and workspaces.")
app.add_typer(workspace_app, name="workspace")
sandbox_app = typer.Typer(no_args_is_help=True, help="Manage per-evaluation sandbox containers.")
app.add_typer(sandbox_app, name="sandbox")

PromptDBPath = Annotated[
    Path,
    typer.Option("--promptdb", exists=True, dir_okay=False, help="Path to PromptDB.md."),
]
Branch = Annotated[str, typer.Argument(help="Eval branch, e.g. eval/opencode/lumen--glm-5.3.")]
Ports = Annotated[
    list[str] | None,
    typer.Option(
        "--port", "-p", help="PORT or HOST_PORT:PORT to forward from localhost (repeatable)."
    ),
]
IncludeStep43 = Annotated[
    bool,
    typer.Option("--include-step-43", help="Include the 'Not yet committed' follow-up step."),
]
HarnessOption = Annotated[str, typer.Option("--harness", help="Harness: claude or opencode.")]
ModelOption = Annotated[
    str, typer.Option("--model", help="Model id, e.g. claude-sonnet-5-5 or lumen/glm-5.3.")
]
EffortOption = Annotated[
    str | None, typer.Option("--effort", help="Effort level (part of the branch name).")
]
DockerBuild = Annotated[
    bool,
    typer.Option("--docker-build", help="Also run the (slow) docker build check from step 14."),
]


@prompts_app.command("list")
def list_prompts(
    path: PromptDBPath = Path("PromptDB.md"),
    include_step_43: IncludeStep43 = False,
) -> None:
    """List every step with its phase and source commits."""
    db = promptdb.load(path, include_step_43)
    phase = None
    for step in db.steps:
        if step.phase != phase:
            phase = step.phase
            typer.echo(f"\nPhase {step.phase} — {step.phase_title}")
        line = f"  {step.number:>2}  {step.title}"
        if step.source_commits:
            line += f"  [{', '.join(step.source_commits)}]"
        typer.echo(line)
    typer.echo(f"\n{len(db.steps)} steps, PromptDB.md sha256 {db.sha256[:12]}")


@prompts_app.command("show")
def show_prompt(
    number: Annotated[int, typer.Argument(help="Step number.")],
    harness: Annotated[str, typer.Option(help="Harness name for the Assisted-by trailer.")] = (
        "<harness>"
    ),
    model: Annotated[str, typer.Option(help="Model id for the Assisted-by trailer.")] = "<model>",
    path: PromptDBPath = Path("PromptDB.md"),
    include_step_43: IncludeStep43 = False,
) -> None:
    """Print the full prompt the agent receives for a step."""
    db = promptdb.load(path, include_step_43)
    try:
        step = db.step(number)
    except KeyError as e:
        raise typer.BadParameter(str(e.args[0]), param_hint="NUMBER") from None
    typer.echo(db.build_prompt(step, harness, model), nl=False)


@workspace_app.command("open")
def open_workspace(
    harness: Annotated[str, typer.Option(help="Harness: claude or opencode.")],
    model: Annotated[str, typer.Option(help="Model id, e.g. claude-sonnet-5-5 or lumen/glm-5.3.")],
    effort: Annotated[str | None, typer.Option(help="Effort level (part of the branch name).")] = (
        None
    ),
) -> None:
    """Create (or restore from the main repo) an evaluation's workspace and branch."""
    try:
        evaluation = branches.Evaluation(harness, model, effort)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from None
    repo = branches.repo_root(Path.cwd())
    try:
        workspace = branches.open_workspace(repo, evaluation)
        branches.sync_to_main(repo, workspace)
    except (branches.WorkspaceError, branches.GitError) as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from None
    typer.echo(f"workspace  {workspace.path.relative_to(repo)}")
    typer.echo(f"branch     {evaluation.branch}")
    typer.echo(f"next step  {workspace.next_step()}")


@workspace_app.command("list")
def list_workspaces() -> None:
    """List the eval branches in the main repository and the next step of each."""
    repo = branches.repo_root(Path.cwd())
    names = branches.eval_branches(repo)
    if not names:
        typer.echo("no eval branches")
    for name in names:
        typer.echo(f"{name}  next step {branches.next_step(repo, name)}")


def _sandbox(branch: str) -> sandbox.Sandbox:
    try:
        return sandbox.Sandbox.for_branch(branches.repo_root(Path.cwd()), branch)
    except ValueError as e:
        raise typer.BadParameter(str(e), param_hint="BRANCH") from None


def _fail(e: Exception) -> typer.Exit:
    typer.echo(f"error: {e}", err=True)
    return typer.Exit(1)


def _forwards(specs: list[str]) -> list[sandbox.Forward]:
    try:
        return [sandbox.Forward.parse(spec) for spec in specs]
    except ValueError as e:
        raise typer.BadParameter(str(e), param_hint="--port") from None


def _echo_forwards(forwards: list[sandbox.Forward]) -> None:
    for forward in forwards:
        typer.echo(f"{forward.url}  ->  sandbox port {forward.port}")


@app.command("run")
def run(
    harness: Annotated[str, typer.Option(help="Harness: claude or opencode.")],
    model: Annotated[str, typer.Option(help="Model id, e.g. claude-sonnet-5-5 or lumen/glm-5.3.")],
    effort: Annotated[str | None, typer.Option(help="Effort level (part of the branch name).")] = (
        None
    ),
    steps: Annotated[
        str, typer.Option(help="'next', a step N, or a range N-M (from the branch's next step).")
    ] = "next",
    until: Annotated[
        int | None, typer.Option(help="Run from the first selected step through this one.")
    ] = None,
    timeout: Annotated[float, typer.Option(help="Wall-clock limit per step, in minutes.")] = 60,
    on_failure: Annotated[
        str, typer.Option(help="After a failed step: continue or stop.")
    ] = "continue",
    retry: Annotated[
        bool,
        typer.Option(
            help="Give a failed or timed-out step, or one that breaks a check, one retry in a "
            "fresh session."
        ),
    ] = True,
    run_checks: Annotated[
        bool, typer.Option("--checks/--no-checks", help="Run the objective checks after each step.")
    ] = True,
    docker_build: DockerBuild = False,
    min_budget: Annotated[
        float,
        typer.Option(
            help="Wait before each session until the provider's spend budget, if it reports one "
            "(Lumen's coins), is at least this."
        ),
    ] = runner.Options.min_budget,
    reset: Annotated[
        bool, typer.Option("--reset", help="Discard steps that already ran and rerun them.")
    ] = False,
    path: PromptDBPath = Path("PromptDB.md"),
    include_step_43: IncludeStep43 = False,
) -> None:
    """Run benchmark steps for one (harness, model, effort) pair, one session per step."""
    try:
        evaluation = branches.Evaluation(harness, model, effort)
        driver = harnesses.get(harness)
        options = runner.Options(
            timeout_s=timeout * 60, on_failure=on_failure, retry_failed=retry,
            min_budget=min_budget,
        )  # fmt: skip
    except ValueError as e:
        raise typer.BadParameter(str(e)) from None
    repo = branches.repo_root(Path.cwd())
    db = promptdb.load(path, include_step_43)
    tracking.load_env(repo)
    try:
        tracker = tracking.Tracker.from_env()
        box = sandbox.Sandbox.for_branch(repo, evaluation.branch)
        check_list = checks.default_checks(docker_build) if run_checks else []
        job = runner.Runner(
            repo, evaluation, db, driver, box, tracker, options, log=typer.echo,
            check_list=check_list,
        )  # fmt: skip
        selected = runner.select_steps(steps, until, job.workspace.next_step(), len(db.steps))
        outcomes = job.run(selected, reset=reset)
    except ValueError as e:
        raise typer.BadParameter(str(e), param_hint="--steps/--until") from None
    except (
        tracking.TrackingError,
        runner.RunnerError,
        checks.CheckError,
        branches.WorkspaceError,
        branches.GitError,
        sandbox.DockerError,
        sandbox.SandboxError,
    ) as e:
        raise _fail(e) from None
    if any(outcome.status != harnesses.base.OK for outcome in outcomes):
        raise typer.Exit(1)


@app.command("check")
def check(
    harness: HarnessOption,
    model: ModelOption,
    effort: EffortOption = None,
    step: Annotated[
        int | None,
        typer.Option(help="Run the checks that apply to this step (default: the latest step)."),
    ] = None,
    docker_build: DockerBuild = False,
) -> None:
    """Run the objective checks on an evaluation's workspace as it is, without logging to MLflow.

    They run on a copy of the workspace; logs go to runs/<branch>/check.checks/.
    """
    try:
        evaluation = branches.Evaluation(harness, model, effort)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from None
    repo = branches.repo_root(Path.cwd())
    try:
        workspace = branches.Workspace(branches.workspace_path(repo, evaluation), evaluation)
        step = step if step is not None else workspace.next_step() - 1
        if step < 1:
            raise typer.BadParameter(f"no step has run on {evaluation.branch}", param_hint="--step")
        box = sandbox.Sandbox.for_branch(repo, evaluation.branch)
        for action in box.up():
            typer.echo(action)
        log_dir = repo / "runs" / evaluation.branch / "check.checks"
        results = checks.run_checks(
            box, workspace.path, step, checks.default_checks(docker_build), log_dir, typer.echo
        )
    except (
        checks.CheckError,
        branches.WorkspaceError,
        branches.GitError,
        sandbox.DockerError,
        sandbox.SandboxError,
    ) as e:
        raise _fail(e) from None
    for result in results:
        typer.echo(f"  {result.status:<8} {result.check.title}")
    typer.echo(f"logs in {log_dir.relative_to(repo)}")
    if any(result.status == checks.FAILED for result in results):
        raise typer.Exit(1)


@sandbox_app.command("build")
def build_sandbox(
    pull: Annotated[bool, typer.Option(help="Pull newer base images first.")] = False,
) -> None:
    """Build the sandbox image from sandbox/Dockerfile."""
    repo = branches.repo_root(Path.cwd())
    try:
        sandbox.check_docker()
        sandbox.build_image(repo / "sandbox", pull=pull)
    except sandbox.DockerError as e:
        raise _fail(e) from None
    typer.echo(f"built {sandbox.image_tag()}")


@sandbox_app.command("up")
def sandbox_up(branch: Branch) -> None:
    """Start an evaluation's sandbox (dind and agent containers), creating what is missing."""
    box = _sandbox(branch)
    try:
        actions = box.up()
    except (sandbox.DockerError, sandbox.SandboxError) as e:
        raise _fail(e) from None
    for action in actions or [f"sandbox {box.name} already running"]:
        typer.echo(action)


@sandbox_app.command("down")
def sandbox_down(branch: Branch) -> None:
    """Remove an evaluation's containers, network, and dind volume (the workspace is kept)."""
    box = _sandbox(branch)
    try:
        removed = box.down()
    except sandbox.DockerError as e:
        raise _fail(e) from None
    for item in removed or [f"no sandbox {box.name}"]:
        typer.echo(f"removed {item}" if removed else item)


@sandbox_app.command(
    "shell", context_settings={"allow_extra_args": True, "ignore_unknown_options": True}
)
def sandbox_shell(ctx: typer.Context, branch: Branch) -> None:
    """Open a shell in the agent container (or run the command given after the branch)."""
    box = _sandbox(branch)
    try:
        code = box.shell(ctx.args) if ctx.args else box.shell()
    except (sandbox.DockerError, sandbox.SandboxError) as e:
        raise _fail(e) from None
    raise typer.Exit(code)


@sandbox_app.command("list")
def list_sandboxes() -> None:
    """List the sandboxes on this Docker host and the state of their containers."""
    try:
        sandbox.check_docker()
        found = sandbox.list_sandboxes()
    except sandbox.DockerError as e:
        raise _fail(e) from None
    if not found:
        typer.echo("no sandboxes")
    for name, containers in sorted(found.items()):
        states = "  ".join(f"{c} {state}" for c, state in sorted(containers.items()))
        typer.echo(f"{name}  {states}")


@sandbox_app.command("forward")
def sandbox_forward(
    branch: Branch,
    ports: Annotated[
        list[str] | None, typer.Argument(help="PORT or HOST_PORT:PORT; all forwarders if --stop.")
    ] = None,
    stop: Annotated[bool, typer.Option("--stop", help="Remove forwarders instead.")] = False,
) -> None:
    """Forward sandbox ports to localhost until stopped (or the sandbox goes down).

    The server in the sandbox must listen on all interfaces, e.g. vite --host 0.0.0.0.
    """
    box = _sandbox(branch)
    forwards = _forwards(ports or [])
    try:
        if stop:
            removed = box.unforward([forward.host_port for forward in forwards])
            for name in removed or ["no forwarders"]:
                typer.echo(f"removed {name}" if removed else name)
            return
        if not forwards:
            raise typer.BadParameter("give at least one port", param_hint="PORTS")
        box.forward(forwards)
    except (sandbox.DockerError, sandbox.SandboxError) as e:
        raise _fail(e) from None
    _echo_forwards(forwards)


@app.command("serve", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def serve_app(ctx: typer.Context, branch: Branch, ports: Ports = None) -> None:
    """Run an implementation's app in its sandbox, forwarded to localhost. Ctrl-C stops it.

    Default: docker compose up --build (ports 3000, 9001) with a compose file.

    Without one: the SvelteKit dev server in web/ (port 5173).

    Or give a command after --; it runs in /workspace and must listen on all interfaces.
    """
    box = _sandbox(branch)
    if ctx.args:
        launch = serve.Launch(ctx.args)
    else:
        try:
            launch = serve.default_launch(box.workspace)
        except serve.ServeError as e:
            raise _fail(e) from None
    forwards = _forwards(ports) if ports else launch.forwards
    try:
        for action in box.up():
            typer.echo(action)
        box.forward(forwards)
    except (sandbox.DockerError, sandbox.SandboxError) as e:
        raise _fail(e) from None
    _echo_forwards(forwards)
    try:
        code = box.shell(launch.command, workdir=launch.workdir)
    finally:
        box.unforward([forward.host_port for forward in forwards])
    raise typer.Exit(code)
