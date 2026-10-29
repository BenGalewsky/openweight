# openweight — Coding-Agent Evaluation Framework: Plan

## Goal

Measure how well different **coding harnesses** (Claude Code, OpenCode) paired with different
**models** can build a realistic application from a sequence of prompts. The benchmark task is
`PromptDB.md`: 42 ordered steps that rebuild the ISGS/IDOT Wetlands Water Database (a SvelteKit +
Drizzle web app and a Dagster ingestion backend).

For each `(harness, model)` pair we:

1. Run every prompt in order, one agent session per step, on a dedicated git branch.
2. Commit the result of each step to that branch.
3. Log each step (traces, cost, timing, objective checks) to MLflow GenAI.
4. On request, have a frontier model review the implementations and log quality scores to MLflow,
   so pairs can be compared step by step and overall.

## Repository layout

`main` holds only the framework. Implementation branches never contain framework code.

```
main
├── PromptDB.md                 # benchmark prompts (source of truth)
├── PLAN.md, README.md
├── db/
│   ├── IDOT.accdb              # gitignored: source Access DB (benchmark input)
│   ├── Wetlands_IDOT_BE.accdb  # gitignored: source Access DB (benchmark input, ~775 MB)
│   └── access_db_schema.sql    # tracked: mdb-schema dump, given to agents as plans/access_db_schema.sql
├── plans/                      # tracked: design docs given to agents (committed into every branch)
│   ├── ptd_review_window.md    #   referenced by step 28
│   └── samples-redesign.md     #   referenced by step 35
├── pyproject.toml / uv.lock    # uv-managed Python project
├── src/openweight/
│   ├── cli.py                  # `openweight` entrypoint (typer)
│   ├── promptdb.py             # parse PromptDB.md -> Step objects
│   ├── branches.py             # branch naming, worktrees, step tracking via git trailers
│   ├── harnesses/
│   │   ├── base.py             # Harness protocol + StepResult
│   │   ├── claude.py           # `claude -p` driver
│   │   └── opencode.py         # `opencode run` driver
│   ├── checks.py               # objective checks run after each step
│   ├── tracking.py             # MLflow experiment/run/trace helpers
│   └── review/
│       ├── rubric.py           # scoring rubric + judge prompts
│       └── judge.py            # frontier-model review -> MLflow assessments
├── harness_config/
│   ├── claude.settings.json    # MLflow tracing hooks + permissions for headless runs
│   ├── opencode.json           # @mlflow/opencode plugin + permissions
│   └── mcp.json                # Svelte MCP server (required by the prompts)
├── data/                       # tracked: logger fixtures (Diver, Aqua TROLL, VuSitu) given to agents
│                               #   Access JSON extracts are never stored here (gitignored)
└── tests/                      # unit tests for the framework itself
workspaces/                     # gitignored: one git worktree per eval branch
```

## Branch model

- Branch name: `eval/<harness>/<model-slug>`, e.g. `eval/claude/claude-sonnet-5-5`,
  `eval/opencode/openrouter--qwen3-coder`. The slug lowercases the model id and replaces `/`
  and `:` with `--` so OpenCode `provider/model` ids are valid ref names.
  (The `eval/` prefix avoids ref conflicts such as `claude` vs `claude/...`.)
- Each branch is an **orphan** branch whose root commit (`chore(eval): benchmark context`) contains
  only the `plans/` directory: every file in `plans/` on `main` (`ptd_review_window.md`,
  `samples-redesign.md`) plus `access_db_schema.sql` copied from `db/`. This keeps framework code
  out of the agent's context and out of the diffs the reviewer sees, while giving every agent the
  design docs that steps 28 and 35 reference and the Access schema from the first step.
- The runner works in a git worktree at `workspaces/<branch-slug>/`, so `main` stays checked out
  and several evaluations can run in parallel.
- **Benchmark inputs.** PromptDB expects `IDOT.accdb` and `Wetlands_IDOT_BE.accdb` at the repo
  root. When the runner creates a worktree it places both from `db/` there as copy-on-write
  clones (`cp -c` on APFS, plain copy elsewhere), so the large file costs no extra disk and the
  agent can never modify the originals.
- **Logger fixtures.** Every file in `data/` on `main` (`DIVER_49_5L_GW_20241002.CSV`,
  `INS_49_SW43_SW_20250924.csv`, `VuSitu_Low-Flow_*.csv` and its `.html` companion) is copied into
  the worktree's `data/`, where steps 23, 29, 36 and 37 expect them.
- The Access DBs and the logger fixtures are listed in the worktree's `.git/info/exclude`, so they
  are never committed regardless of what `.gitignore` the agent writes (and step 10's "stop
  tracking seed data" has nothing of ours to remove). The runner fails fast if any input is
  missing.
- **No JSON extracts are supplied.** The `data/*.json` NDJSON files the seeding steps read
  (Projects.json, Visits.json, LUTC_Initials.json, SO_Read.json, ...) must come from the exporter
  the agent writes in step 1, run against the supplied Access DBs. If an agent's exporter is wrong
  or incomplete, its later seeding steps live with that; the seed check and the reviewer will see
  the consequences. Agent-produced extracts stay in the worktree for the life of the branch.
- Because `plans/access_db_schema.sql` is already present, step 3 ("dump the schema into
  plans/access_db_schema.sql") is effectively done before it runs. The framework preamble for that
  step tells the agent the file was supplied, so it should verify it against the databases rather
  than regenerate it.
- Harness configuration is **not** written into the worktree: Claude Code gets it via
  `--settings` / `--mcp-config`, OpenCode via the `OPENCODE_CONFIG` env var. Nothing
  framework-specific leaks into commits.

### Tracking progress in git

Every step commit carries trailers, so the branch itself is the state — no local state file:

```
feat(web): scaffold SvelteKit wetlands app

Assisted-by: claude:claude-sonnet-5-5
Eval-Step: 1
Eval-Run-Id: <mlflow run id>
```

The runner also creates a lightweight tag `eval/<harness>/<model-slug>/step-01` per step so the
reviewer can diff `step-(N-1)..step-N` exactly.

The next step to run = highest `Eval-Step` trailer on the branch + 1.

## Prompt parsing (`promptdb.py`)

- Split `PromptDB.md` on `Step N — Title (shas)` headings, capturing `number`, `title`,
  `phase`, `source_commits`, and `body`.
- The **Conventions** preamble (commit style, `Assisted-by` trailer, dev DB, Svelte MCP) is
  prepended to every step prompt, with `<harness>:<model>` filled in.
- The "Reverted history" note and the "Not yet committed / step 43" section are excluded by
  default (`--include-step-43` to opt in).
- Commit SHAs in the prompts refer to a repository the agent does not have. The framework adds
  a short preamble telling the agent those SHAs are informational only.
- `openweight prompts list` / `openweight prompts show 12` to inspect the parsed result.

## Runner (`openweight run`)

```
uv run openweight run --harness claude --model claude-sonnet-5-5 [--steps 1-10] [--until 42]
uv run openweight run --harness opencode --model openrouter/qwen/qwen3-coder --steps next
```

For each step:

1. Ensure the worktree is clean and at the previous step's tag.
2. Build the prompt (conventions + step body + framework preamble).
3. Start an MLflow run (nested under the branch's parent run) and invoke the harness:
   - **Claude Code:** `claude -p "<prompt>" --model <model> --output-format stream-json
     --settings harness_config/claude.settings.json --mcp-config harness_config/mcp.json
     --permission-mode bypassPermissions --max-budget-usd <cap>`, cwd = worktree.
   - **OpenCode:** `opencode run --standalone --model <provider/model> --format json --auto
     "<prompt>"`, cwd = worktree, `OPENCODE_CONFIG=harness_config/opencode.json`.
   - Fresh session per step (the previous steps' context lives in the code and git history,
     as it would for a human developer picking up the next ticket).
   - Wall-clock timeout per step (default 60 min) plus a cost cap where the harness supports it.
4. Capture the JSON event stream to `runs/<branch>/step-NN.jsonl` and log it as an MLflow artifact.
5. If the agent left uncommitted changes, the runner commits them itself
   (`chore(eval): uncommitted changes from step N`) and records `agent_committed=false`.
   If the agent made no commits at all, an empty commit with the trailers is created so the step
   is still recorded.
6. Run objective checks (below), log results, tag the step, move on.
7. On harness failure/timeout: record `status=failed`, commit what exists, and continue or stop
   according to `--on-failure {continue,stop}` (default `continue`, so later steps still get
   a score even when one step went badly).

## Objective checks (`checks.py`)

Run in the worktree after each step; each result is a 0/1 metric plus captured logs. Checks are
skipped (not failed) until the step that introduces the relevant tooling.

| Check | Command | From step |
|-------|---------|-----------|
| web install | `npm ci` (in `web/`) | 1 |
| web typecheck | `npm run check` | 1 |
| web lint | `npm run lint` | 1 |
| web build | `npm run build` | 1 |
| db push + seed | `npm run db:push && npm run db:seed` against a throwaway Postgres | 15 |
| compose config | `docker compose config` | 16 |
| dagster install | `uv sync` (in `isgs_dagster/`) | 21 |
| dagster defs load | `uv run dg check defs` | 21 |
| dagster tests | `uv run pytest` | 23 |
| docker build | `docker build --target runtime .` (opt-in, slow) | 14 |

Also logged: files changed, lines added/removed, number of commits by the agent, whether the
conventional-commit format and `Assisted-by` trailer were followed.

## MLflow tracking (`tracking.py`)

- Configuration from `.env` (loaded with `python-dotenv`): `MLFLOW_TRACKING_URI`,
  `MLFLOW_TRACKING_USERNAME`, `MLFLOW_TRACKING_PASSWORD`, `MLFLOW_EXPERIMENT_ID`. These are also
  passed through to the harness subprocesses so their tracing hooks/plugins reach the server and
  log into the same experiment.
- Experiment: everything (runs, traces, review assessments) is logged to the existing experiment
  given by `MLFLOW_EXPERIMENT_ID`. The runner fails fast at startup if it is unset or the
  experiment does not exist on the server; it never creates experiments itself.
- Every run is tagged `promptdb_sha` (hash of PromptDB.md) so results from different prompt
  revisions in the same experiment can be filtered apart.
- **Parent run** per branch: tags `harness`, `model`, `branch`, `harness_version`, `promptdb_sha`.
- **Child run** per step: params `step`, `title`, `phase`; metrics `duration_s`, `cost_usd`,
  `input_tokens`, `output_tokens`, `num_turns`, `tool_calls`, check results, diff stats; artifacts:
  prompt, event stream, check logs, `git diff step-(N-1)..step-N`.
- **Traces** (MLflow GenAI):
  - Claude Code: MLflow's Claude Code hook integration (`mlflow autolog claude` configures
    Stop hooks); we put the equivalent hook config in `harness_config/claude.settings.json`.
  - OpenCode: the `@mlflow/opencode` plugin listed in `harness_config/opencode.json`.
  - The step's MLflow run id is passed via env so traces are linked to the step run
    (to verify during implementation; fallback is tagging traces by session id after the fact).
- Token/cost metrics come from the harness's own JSON output, so they are comparable even if
  tracing coverage differs between harnesses.

## Review (`openweight review`)

Run on request, separately from implementation:

```
uv run openweight review --branch eval/claude/claude-sonnet-5-5 [--steps 1-42] [--judge <model>]
uv run openweight review --all            # every eval/* branch, for a leaderboard
```

- Input per step: the step prompt, the diff `step-(N-1)..step-N`, check results, and read access
  to the worktree at that tag (the judge runs as an agent with read-only tools so it can inspect
  files beyond the diff).
- The judge is **blind**: harness/model names and `Assisted-by` trailers are stripped from what
  it sees.
- Rubric (each 1–5 with a written rationale):
  - **Completeness** — every requirement in the step prompt is implemented.
  - **Correctness** — the code would work; no obvious bugs; checks pass.
  - **Code quality** — idiomatic Svelte 5 / Drizzle / Dagster, readable, no dead code.
  - **Consistency** — fits the existing codebase; no regressions of earlier steps.
  - **Scope discipline** — no unrequested features, no reproduced reverted history.
  - **Process** — commit messages, trailers, tests added where asked.
- Scores are logged as MLflow **assessments/feedback** on the step's trace and as metrics on the
  step run (`review.<criterion>`), with the judge model recorded as the source. Implemented with
  `mlflow.genai` custom judges so the scorers can also be rerun via `mlflow.genai.evaluate`.
- A final whole-branch review scores the end state (architecture, overall app quality) once all
  steps are done.
- `openweight report` produces a comparison table (per pair: total cost, time, check pass rate,
  mean review scores, per-phase breakdown) from MLflow.

## Implementation milestones

1. **Skeleton** — `uv init`, `pyproject.toml` (mlflow, typer, python-dotenv, gitpython or plain
   `git` subprocess), `promptdb.py` with tests that it parses all 42 steps.
2. **Branches** — orphan branch + worktree creation, trailer parsing, step tags.
3. **Claude harness** — run step 1 end to end with Claude Code; MLflow run + trace visible on the
   server.
4. **OpenCode harness** — same for OpenCode; normalize its JSON events into the same
   `StepResult` (tokens, cost, turns).
5. **Checks** — objective checks with per-step gating; throwaway Postgres via docker.
6. **Full run** — one complete 42-step run per harness with a baseline model; fix runner
   robustness issues (timeouts, failed steps, resume).
7. **Review** — judge, rubric, MLflow assessments, `report` command.
8. **Matrix** — config file listing `(harness, model)` pairs; `openweight run --matrix`.

## Open questions

To resolve before milestone 3.

1. **Model matrix.** Which models for each harness? For OpenCode, which providers (OpenRouter,
   local Ollama/vLLM, a hosted endpoint)? Do we need a matched-model control (e.g. the same Claude
   model under both harnesses) to separate harness effects from model effects?
2. **Repeat runs.** Agent runs are non-deterministic. Is one run per pair enough, or do we want
   N repeats? If repeats, the branch name needs a suffix (`eval/claude/<model>/r2`).
3. **Session strategy.** Plan assumes a fresh session per step. Alternative: one continuous
   session per branch (tests long-context handling but cannot be compared fairly across
   harnesses). OK with fresh-per-step?
4. **Failure policy.** If step N fails or breaks the build, keep going (current default), stop, or
   let the agent retry once with the check output?
5. **Sandboxing.** Headless runs need permissions bypassed (`bypassPermissions` / `--auto`), and the
   prompts need Docker, Postgres, npm, and uv. Run on the host as-is, or inside a container/VM per
   evaluation? This also affects whether evaluations can run in parallel (port 5432/9000 clashes).
6. **MLflow server.** `MLFLOW_EXPERIMENT_ID` in `.env` needs to be filled in with the experiment
   created on the server. Is the tracking server new enough for GenAI traces and assessments (MLflow ≥ 3.4)?
7. **Judge model and cadence.** Which frontier model is the reviewer (and should we use two judges
   from different vendors to reduce self-preference bias, since Claude would be judging Claude)?
   Review every step, only phase boundaries, or only the final state?
8. **Budget.** Per-step and per-run cost/time caps? 42 steps × several pairs × possible repeats
    adds up quickly.
9. **Remote.** Should `eval/*` branches be pushed to a GitHub remote (for sharing/inspection),
    or stay local? There is currently no remote.
10. **Step 43.** Include the "not yet committed" follow-up as a final step, or leave it out?
