"""The `openweight` command line."""

from pathlib import Path
from typing import Annotated

import typer

from openweight import promptdb

app = typer.Typer(no_args_is_help=True, help="Evaluate coding harnesses and models on PromptDB.")
prompts_app = typer.Typer(no_args_is_help=True, help="Inspect the parsed benchmark prompts.")
app.add_typer(prompts_app, name="prompts")

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
