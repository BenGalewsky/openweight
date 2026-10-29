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
uv run pytest                               # framework tests
```

## Status

Milestone 1 (skeleton and PromptDB parsing) is done. The runner, sandbox, and reviewer are not
implemented yet; see the milestones in [PLAN.md](PLAN.md).
