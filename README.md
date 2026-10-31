# openweight

A framework for evaluating coding harnesses (Claude Code, OpenCode) and models on a realistic,
multi-step build: the ordered prompts in [PromptDB.md](PromptDB.md) rebuild a SvelteKit web app
and a Dagster data pipeline from scratch.

Each `(harness, model)` pair implements the prompts one at a time on its own branch
(`eval/<harness>/<model-slug>`), committing after each step. Runs are tracked in MLflow GenAI, and
a frontier model can be asked to review and score the implementations.

See [PLAN.md](PLAN.md) for the design and open questions.

## Setup

```bash
cp .env.example .env   # then fill in MLflow credentials
```

Place the source Access databases in `db/` (they are gitignored):

```
db/IDOT.accdb
db/Wetlands_IDOT_BE.accdb
```

Requires on the host: `git`, `uv`, and Docker (OrbStack or Docker Desktop). Agents and checks run
in a per-evaluation sandbox container that carries everything else (Node, mdbtools, the `claude`
and `opencode` CLIs). The models under test are listed in [matrix.toml](matrix.toml).

## Usage

```bash
uv run openweight prompts list              # the 42 parsed steps by phase
uv run openweight prompts show 12 --harness claude --model claude-sonnet-5-5
uv run openweight workspace open --harness claude --model claude-sonnet-5-5 --effort high
uv run openweight workspace list            # eval branches and the next step of each
uv run openweight sandbox build             # build the agent sandbox image (once per pin change)
uv run openweight sandbox up eval/claude/claude-sonnet-5-5-high
uv run openweight sandbox shell eval/claude/claude-sonnet-5-5-high
uv run openweight sandbox down eval/claude/claude-sonnet-5-5-high
uv run openweight sandbox list              # sandboxes on this Docker host
uv run openweight run --harness claude --model claude-sonnet-5-5 --effort high --steps 1
uv run openweight run --harness claude --model claude-sonnet-5-5 --effort high --until 10
uv run openweight run --harness claude --model claude-sonnet-5-5 --effort high --steps 1 --reset
uv run openweight run --harness opencode --model lumen/glm-5.3 --steps 1
uv run pytest                               # framework tests
uv run pytest --docker                      # also start real sandbox containers
```

## Status

Milestones 1 (skeleton and PromptDB parsing), 2 (eval branches and workspaces), 3 (sandbox), 4
(Claude harness), and 5 (OpenCode harness) are done. `workspace open` creates the orphan eval branch
in `workspaces/<harness>--<model-slug>/` with the benchmark inputs in place, and `sandbox up` starts
that evaluation's agent container and private Docker daemon around it. `run` drives Claude Code or
OpenCode through the steps in that sandbox: each step becomes a tagged commit on the branch and a
child run of the branch's MLflow run, with the session's trace linked to it and the event stream
kept in `runs/<branch>/`. OpenCode traces are written by the runner from OpenCode's session
exports, as the `@mlflow/opencode` plugin does not load in OpenCode 2. Objective checks and the
reviewer are not implemented yet; see the milestones in [PLAN.md](PLAN.md).
