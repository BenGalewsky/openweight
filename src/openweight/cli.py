"""The `openweight` command line."""

from pathlib import Path
from typing import Annotated

import typer

from openweight import branches, promptdb, sandbox

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
IncludeStep43 = Annotated[
    bool,
    typer.Option("--include-step-43", help="Include the 'Not yet committed' follow-up step."),
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
