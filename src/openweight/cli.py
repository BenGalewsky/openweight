"""The `openweight` command line."""

from pathlib import Path
from typing import Annotated

import typer

from openweight import branches, promptdb

app = typer.Typer(no_args_is_help=True, help="Evaluate coding harnesses and models on PromptDB.")
prompts_app = typer.Typer(no_args_is_help=True, help="Inspect the parsed benchmark prompts.")
app.add_typer(prompts_app, name="prompts")
workspace_app = typer.Typer(no_args_is_help=True, help="Manage evaluation branches and workspaces.")
app.add_typer(workspace_app, name="workspace")

PromptDBPath = Annotated[
    Path,
    typer.Option("--promptdb", exists=True, dir_okay=False, help="Path to PromptDB.md."),
]
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
