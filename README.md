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

Requires: `git`, `uv`, `node` 22+, Docker, `mdbtools`, and the `claude` and/or `opencode` CLIs.

## Status

Planning. The runner and reviewer are not implemented yet.
