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
4. On request, have a frontier model review the implementations at the end of each PromptDB phase
   and log quality scores to MLflow, so pairs can be compared phase by phase and overall.

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
├── matrix.toml                 # the (harness, model, effort) pairs to evaluate
├── sandbox/
│   ├── Dockerfile              # agent sandbox image: toolchain + pinned claude/opencode CLIs
│   └── entrypoint.sh           #   waits for the evaluation's dind daemon
├── pyproject.toml / uv.lock    # uv-managed Python project
├── src/openweight/
│   ├── cli.py                  # `openweight` entrypoint (typer)
│   ├── promptdb.py             # parse PromptDB.md -> Step objects
│   ├── branches.py             # branch naming, workspaces, step tracking via git trailers
│   ├── sandbox.py              # per-evaluation containers (agent + dind), exec, teardown
│   ├── harnesses/
│   │   ├── base.py             # Harness protocol + StepResult
│   │   ├── claude.py           # `claude -p` driver
│   │   └── opencode.py         # `opencode run` driver; writes OpenCode traces from session exports
│   ├── runner.py               # step loop: sandbox exec, step commits, MLflow logging
│   ├── checks.py               # objective checks run after each step
│   ├── tracking.py             # MLflow experiment/run/trace helpers
│   └── review/
│       ├── rubric.py           # scoring rubric + judge prompts
│       └── judge.py            # Claude and Codex judge sessions -> MLflow assessments
├── harness_config/
│   ├── claude.settings.json    # enables MLflow tracing, turns off commit attribution
│   ├── opencode.json           # Lumen provider, allow-all permissions, Svelte MCP, Dagster skills
│   ├── mcp.json                # Svelte MCP server (required by the prompts)
│   └── plugins/                # vendored Claude Code plugins (dagster-io/skills @ a9ba4bb, Apache-2.0)
│       ├── dagster-expert/     #   loaded by Claude via --plugin-dir, by OpenCode via "skills" paths
│       └── dignified-python/
├── data/                       # tracked: logger fixtures (Diver, Aqua TROLL, VuSitu) given to agents
│                               #   Access JSON extracts are never stored here (gitignored)
└── tests/                      # unit tests for the framework itself
workspaces/                     # gitignored: one standalone git repo per eval branch
```

## Evaluation matrix (`matrix.toml`)

| Harness | Model | Effort | Branch |
|---------|-------|--------|--------|
| Claude Code | `claude-sonnet-5-5` | high | `eval/claude/claude-sonnet-5-5-high` |
| Claude Code | `claude-opus-5-5` | high | `eval/claude/claude-opus-5-5-high` |
| OpenCode | `lumen/glm-5.3` | provider default | `eval/opencode/lumen--glm-5.3` |
| OpenCode | `lumen/qwen3.8-flash` | provider default | `eval/opencode/lumen--qwen3.8-flash` |
| OpenCode | `lumen/ornith-1.0-35b` | provider default | `eval/opencode/lumen--ornith-1.0-35b` |

- Claude runs always pass `--effort high`. Effort is part of the Claude branch slug, so a later run
  at another effort level gets its own branch instead of colliding.
- The OpenCode models are served by the NCSA **Lumen** endpoint
  (`https://lumen.ncsa.illinois.edu/v1`, OpenAI-compatible). `harness_config/opencode.json` defines
  the `lumen` provider with `apiKey: "{env:LUMEN_API_KEY}"`, so the user's own
  `~/.config/opencode/opencode.json` (which holds the key inline) is never mounted into the sandbox.
  Ornith is declared with `"tool_call": true`: Lumen returns proper tool calls for it (checked
  2026-10-29), and without the flag OpenCode would not offer it any tools at all. (OpenCode 2
  ignores the 1.x `"reasoning": true` flag, so it is not set; reasoning output is parsed anyway.)
- GLM and Ornith are declared text-only (`"modalities": {"input": ["text"]}`), as Lumen's model
  list says they are. Without it OpenCode sends an image the agent reads to the model, and Lumen
  rejects the whole request: GLM's first step-2 session (2026-10-31) died reading screenshots it
  had downloaded. Qwen3.8-flash accepts images on Lumen and is left undeclared.
- Each model's `cost` gives `cache_read` the input price. Lumen bills cached input at the full
  input price, and without the key OpenCode counted it as free: GLM's steps 1–17 (2026-10-31)
  logged $4.07 where Lumen charged about $102, since cached input is nearly all of an agent's
  tokens. The logged OpenCode costs were recomputed from their token metrics on 2026-11-01
  (tagged `openweight.cost_recomputed`).
- The Lumen key has a spend budget in coins (about a dollar each at these prices), reported as
  `coins_available` by `<baseURL>/usage`. When it runs out every request fails with HTTP 429
  `insufficient_quota` ("Coin budget exhausted"). It refills gradually: what was spent seems to
  come back about 24 hours later, up to 100 coins. GLM spends about $5 a step, so a run outlasts
  the budget (see Runner, step 3).
- Ornith runs on 1.0 for the duration of the project.
- At startup the runner checks each OpenCode model id against the provider's model list
  (`<baseURL>/models`, with the provider's settings from `opencode.json`) and refuses to start an
  evaluation for a model that isn't served. Claude Code models have no such list on a subscription;
  Claude Code rejects an unknown model itself.

## Branch model

- Branch name: `eval/<harness>/<model-slug>`, e.g. `eval/claude/claude-sonnet-5-5-high`,
  `eval/opencode/lumen--glm-5.3`. The slug lowercases the model id, replaces `/` and `:` with
  `--` so OpenCode `provider/model` ids are valid ref names, and appends `-<effort>` when an
  effort level is set.
  (The `eval/` prefix avoids ref conflicts such as `claude` vs `claude/...`.)
- One run per pair: steps are not repeated to average out non-determinism, so branch names carry
  no run suffix.
- Branches stay local. The main repo has no remote and the runner never pushes `eval/*` branches
  or step tags anywhere.
- Each branch is an **orphan** branch whose root commit (`chore(eval): benchmark context`) contains
  only the `plans/` directory: every file in `plans/` on `main` (`ptd_review_window.md`,
  `samples-redesign.md`) plus `access_db_schema.sql` copied from `db/`. This keeps framework code
  out of the agent's context and out of the diffs the reviewer sees, while giving every agent the
  design docs that steps 28 and 35 reference and the Access schema from the first step.
- Each evaluation works in a **standalone git repository** at `workspaces/<name>/`, where
  `<name>` is `<harness>--<model-slug>` (e.g. `workspaces/opencode--lumen--glm-5.3/`), created with
  `git init`, not `git worktree`. A worktree's `.git` is a pointer back into the main repo, which
  would both break inside the sandbox and expose `main`'s history; a separate repo contains only
  the eval branch. After each step the runner fetches the branch and its step tags into the main
  repo (with `--prune`, so tags removed by a reset disappear there too), so `main`'s clone holds
  every branch for review. If a workspace is lost, `openweight workspace open` restores it from the
  main repo's branch and tags.
- **Benchmark inputs.** PromptDB expects `IDOT.accdb` and `Wetlands_IDOT_BE.accdb` at the repo
  root. When the runner creates a workspace it places both from `db/` there as copy-on-write
  clones (`cp -c` on APFS, `cp --reflink=auto` on Linux, plain copy elsewhere), so the large file
  costs no extra disk and the agent can never modify the originals. The copies are made `0644` so
  the sandbox's agent user can read them whatever the originals' mode.
- **Logger fixtures.** Every file in `data/` on `main` (`DIVER_49_5L_GW_20241002.CSV`,
  `INS_49_SW43_SW_20250924.csv`, `VuSitu_Low-Flow_*.csv` and its `.html` companion) is copied into
  the workspace's `data/`, where steps 23, 29, 36 and 37 expect them.
- The Access DBs and the logger fixtures are listed in the workspace's `.git/info/exclude`, so they
  are never committed regardless of what `.gitignore` the agent writes (and step 10's "stop
  tracking seed data" has nothing of ours to remove). The runner fails fast if any input is
  missing.
- **No JSON extracts are supplied.** The `data/*.json` NDJSON files the seeding steps read
  (Projects.json, Visits.json, LUTC_Initials.json, SO_Read.json, ...) must come from the exporter
  the agent writes in step 1, run against the supplied Access DBs. If an agent's exporter is wrong
  or incomplete, its later seeding steps live with that; the seed check and the reviewer will see
  the consequences. Agent-produced extracts stay in the workspace for the life of the branch.
- Because `plans/access_db_schema.sql` is already present, step 3 ("dump the schema into
  plans/access_db_schema.sql") is effectively done before it runs. The framework preamble for that
  step tells the agent the file was supplied, so it should verify it against the databases rather
  than regenerate it.
- Harness configuration is **not** written into the workspace: it is mounted read-only into the
  sandbox at `/harness_config`. Claude Code gets it via `--settings` / `--mcp-config` /
  `--plugin-dir`, OpenCode via the `OPENCODE_CONFIG` env var. Nothing framework-specific leaks
  into commits.
- Both harnesses get the same Svelte MCP server and the same pinned `dagster-expert` and
  `dignified-python` skills from `harness_config/`, rather than whatever is installed in the
  user's own setup. OpenCode resolves relative `skills` paths against the cwd (the workspace), so
  `opencode.json` uses `{env:OPENWEIGHT_HARNESS_CONFIG}`, which the sandbox sets to
  `/harness_config`.

## Sandbox (`sandbox/`, `sandbox.py`)

Every agent session and every objective check runs inside a container, never on the host. The
host only runs the `openweight` orchestrator, git bookkeeping, and MLflow logging. Docker here is
OrbStack (linux/arm64).

**Per evaluation** the runner starts two containers on a private network, `ow-<name>`:

- **`ow-<name>-dind`** — a `docker:dind` daemon dedicated to this evaluation. The agent's
  `docker compose up` (Postgres, RustFS from step 16 on) and `docker build` (steps 14, 16, 42)
  talk to this daemon, never to the host's. The host Docker socket is never mounted. It runs
  `--privileged` (required by dind), a contained trade-off: it can reach only its own storage
  volume (`ow-<name>-dind`) and the private network. `dockerd` listens on its unix socket and on
  `tcp://127.0.0.1:2375` only; plain TCP is safe there because the agent is the only other process
  in that network namespace, and it avoids sharing a root-owned socket with the non-root agent.
- **`ow-<name>-agent`** — the `openweight-sandbox` image, started with
  `--network container:ow-<name>-dind` so it shares the dind container's network namespace.
  Ports that compose publishes are therefore on `localhost` inside the agent, which keeps
  PromptDB's `DATABASE_URL=postgresql://idot:idot@localhost:5432/idot` working unchanged, and
  parallel evaluations never clash on 5432/9000/3000. The image sets
  `DOCKER_HOST=tcp://127.0.0.1:2375`.

**Image (`openweight-sandbox`)** built from `sandbox/Dockerfile` by `openweight sandbox build`:
Debian 13 with Node 22 (`node:22-trixie-slim`), `uv` with Python 3.13 preinstalled (as steps 1
and 21 require), `mdbtools` (`mdb-tables`, `mdb-json`), `postgresql-client`, `git`, node-gyp's
toolchain (`g++`, `make`, `python3`: `npm ci` builds `better-sqlite3` 13 from source even though
the package ships prebuilt binaries, so without it no step-1 app installs from its lock file), the
Docker CLI with compose and buildx plugins, the `@mlflow/claude-code` tracing plugin (pinned by
`MLFLOW_CLAUDE_CODE_VERSION`; see MLflow tracking), and the harness CLIs
pinned to the versions on the host (`claude` 2.1.312, `opencode` 2.0.23). OpenCode 2 is published
on npm as `@opencode/cli` (the `opencode-ai` package stops at 1.x). The Dockerfile's `ARG *_VERSION` defaults are the only place the pins are
written; `sandbox.py` reads them to name the tag (`openweight-sandbox:claude-2.1.312-opencode-2.0.23`)
and to report `harness_version`, which the runner logs. Auto-updaters are disabled.
There is no OpenCode tracing plugin in the image: `@mlflow/opencode` 0.4.0 (the latest) is an
OpenCode 1 plugin, and OpenCode 2 refuses to load it ("Plugin must export a default definition
with an id and an effect or setup function"; checked 2026-10-30). The runner writes OpenCode traces
on the host instead (see MLflow tracking), so the image no longer needs the `mlflow` CLI either.

**Mounts**

| Host | Container | Mode |
|------|-----------|------|
| `workspaces/<name>/` | `/workspace` (cwd) | read-write |
| `harness_config/` | `/harness_config` | read-only |

Nothing else from the host is mounted: no home directory, SSH keys, git credentials, or the user's
`~/.claude` / `~/.config/opencode`. The Access DBs and logger fixtures are already copied into the
workspace (see Branch model), so the originals in `db/` and `data/` are out of reach.

**Inside the sandbox**

- Runs as a non-root `agent` user (uid 1000, build arg `AGENT_UID`) with git identity
  `openweight-agent` and `safe.directory=*`, so git accepts the bind-mounted workspace whatever
  uid owns it on the host. (OrbStack maps bind-mount ownership to the accessing user anyway.)
- Permission prompts are disabled, which is what the container boundary is for:
  `claude --dangerously-skip-permissions`, `opencode run --auto` with an allow-all `permissions`
  rule in `opencode.json` (`--auto` alone already allows `.env` files and paths outside the
  workspace; the rule keeps it that way if OpenCode's defaults gain a deny).
- Environment passed in: `CLAUDE_CODE_OAUTH_TOKEN` (Claude runs only; a Claude subscription token
  from `claude setup-token`, as there is no Anthropic API key),
  `LUMEN_API_KEY` (OpenCode runs only), the `MLFLOW_*` variables (Claude runs only, for the tracing
  plugin; OpenCode is traced from the host), and `OPENWEIGHT_HARNESS_CONFIG=/harness_config`. Each
  harness sees only its own model credential.
  Credentials are passed per `docker exec` as `--env NAME` with the value in docker's own
  environment, so they never appear in process arguments or in `docker inspect`.
- Network egress is open (npm, PyPI, Docker Hub, the model APIs, the Svelte MCP server, MLflow).
  An allowlisting proxy can be added later if needed.
- Resource limits per evaluation (defaults: 4 CPUs, 8 GB for the agent, 4 GB for dind).

**Lifecycle.** The containers start before step 1 and persist across steps, so the agent's Postgres
data, `node_modules`, and uv caches survive between sessions as they would on a developer's
machine. Each step is a `docker exec` into the agent container; a step timeout runs the command
under `timeout` inside the container, so the agent process itself is killed. All resources carry
the label `openweight.evaluation=<name>`.

- `openweight sandbox up <branch>` starts the sandbox, creating whatever is missing. It is safe to
  run before every step: running containers are left alone, a stopped dind is started and the agent
  restarted to rejoin its network namespace, and the agent container is recreated if dind was
  recreated or the image tag changed.
- `openweight sandbox down <branch>` removes the containers, network, and dind volume (the
  workspace is kept).
- `openweight sandbox shell <branch> [command...]` opens a shell (or runs a command) as the agent.
- `openweight sandbox list` shows every evaluation's containers on the Docker host.

**Reaching the app.** Nothing in the sandbox is published on the host, and a container that
shares dind's network namespace cannot publish ports itself. `openweight sandbox forward <branch>
[HOST_PORT:]PORT` instead starts an `ow-<name>-fwd-<host port>` container (`alpine/socat`) on the
evaluation's network that listens on `127.0.0.1:<host port>` on the host and relays to
`ow-<name>-dind:<port>`. The server must therefore listen on all interfaces in the sandbox, which
compose-published ports do and `vite dev --host 0.0.0.0` does. `openweight serve <branch>` wraps
this: it brings the sandbox up, forwards the app's ports, and runs the app in the foreground (the
dev server in `web/`, or `docker compose up --build` once the branch has a compose file), removing
the forwarders when it exits. `sandbox down` removes any forwarders left over.

### Tracking progress in git

Every step commit carries trailers, so the branch itself is the state — no local state file:

```
feat(web): scaffold SvelteKit wetlands app

Assisted-by: claude:claude-sonnet-5-5
Eval-Step: 1
Eval-Run-Id: <mlflow run id>
```

`Assisted-by` is the agent's own (PromptDB's conventions ask for it). The runner adds the `Eval-*`
trailers by amending the agent's last commit, leaving its author and message otherwise untouched.
When it commits leftover changes or an empty commit itself (see Runner), it writes the
`Assisted-by` trailer too, as `openweight` with the content attributed to the agent.

The runner also creates a lightweight tag `eval/<harness>/<model-slug>/step-01` per step so the
reviewer can diff `step-(N-1)..step-N` exactly. The root commit is tagged `step-00`, so step 1 is
no special case.

The next step to run = highest `Eval-Step` trailer on the branch + 1.

## Prompt parsing (`promptdb.py`)

- Split `PromptDB.md` on `Step N — Title (shas)` headings, capturing `number`, `title`,
  `phase`, `source_commits`, and `body`.
- The **Conventions** preamble (commit style, `Assisted-by` trailer, dev DB, Svelte MCP) is
  prepended to every step prompt, with `<harness>:<model>` filled in.
- The "Reverted history" note and the "Not yet committed / step 43" section are excluded.
  Evaluations run steps 1–42 only; `--include-step-43` exists for inspecting the parse, not for
  evaluation runs.
- Commit SHAs in the prompts refer to a repository the agent does not have. The framework adds
  a short preamble telling the agent those SHAs are informational only.
- `openweight prompts list` / `openweight prompts show 12` to inspect the parsed result.

## Runner (`openweight run`)

```
uv run openweight run --harness claude --model claude-sonnet-5-5 --effort high [--steps 1-10] [--until 42]
uv run openweight run --harness opencode --model lumen/glm-5.3 --steps next
uv run openweight run --matrix matrix.toml [--parallel 2]
```

For each step:

1. Ensure the workspace is clean and at the previous step's tag, and the sandbox is up.
2. Build the prompt (conventions + step body + framework preamble).
3. Start an MLflow run (nested under the branch's parent run) and invoke the harness with
   `docker exec` in the agent container (cwd `/workspace`):
   - **Claude Code:** `claude --print "<prompt>" --model <model> --effort high
     --output-format stream-json --verbose --session-id <uuid>
     --settings /harness_config/claude.settings.json
     --mcp-config /harness_config/mcp.json --strict-mcp-config --plugin-dir /harness_config/plugins
     --plugin-dir <@mlflow/claude-code> --dangerously-skip-permissions`.
     (`--strict-mcp-config` keeps any other MCP servers out of the run; the runner picks the
     session id so it can find the session's trace afterwards.) `claude.settings.json` turns off
     Claude Code's own commit and PR attribution (`Co-Authored-By`, "Generated with"), so commit
     messages carry only what PromptDB's conventions ask for, as with OpenCode, and the blind
     reviewer is not handed the harness's name.
   - **OpenCode:** `opencode run --standalone --model <provider/model> --format json --auto`,
     with `OPENCODE_CONFIG=/harness_config/opencode.json` and the prompt on stdin: OpenCode wraps
     a message given as an argument in quotes and escapes the double quotes inside it.
     `--standalone` runs a private server, so no background service is left in the container.
     An effort level, if set, becomes the model variant (`provider/model#effort`). OpenCode picks
     its own session id; the runner reads it from the event stream.
   - Fresh session per step (the previous steps' context lives in the code and git history,
     as it would for a human developer picking up the next ticket).
   - Wall-clock timeout per step (default 60 min). No dollar cap: Claude runs on a subscription
     and Lumen's own budget caps OpenCode.
   - Both Claude evaluations share one subscription's usage limits, so `--parallel` never runs
     more than one Claude evaluation at a time (OpenCode evaluations can run alongside). If a Claude
     step hits a usage limit, the runner resets the workspace to the previous step's tag, waits for
     the limit to reset, and reruns the step, recording `rate_limited_retries`. The limit is
     detected from Claude Code's `rate_limit_event` (status `rejected`, with its `resetsAt`) or an
     HTTP 429 in the result; without a reset time the runner waits 15 minutes.
   - For a provider that reports a spend budget (Lumen, through the harness's `budget`), the
     runner waits before every session until at least `--min-budget` (default 25) is left,
     polling every 10 minutes, so a session does not start only to run out partway. A session
     stopped by an exhausted budget (OpenCode reports Lumen's 429 but not its `Retry-After`) is
     discarded and rerun like a usage-limit stop, after waiting for the budget rather than a
     fixed time.
   - A discarded session's tokens and cost still count towards the step (`discarded_cost_usd`
     says how much was thrown away). If the limit outlasts 20 reruns, or the budget stays low for
     26 hours, the runner resets the workspace to the previous step's tag and stops the run,
     tagging the step run `status=error`, rather than record a step that never ran; `--steps
     next` resumes it. (Before this, GLM's step 18 used up its reruns overnight and was recorded
     as an empty `rate_limited` step that passed every check.)
   - `--steps` takes `next` (default), `N`, or `N-M`, and must start at the branch's next step.
     `--reset` reruns earlier steps: it resets the branch to the tag before the first one and tags
     the discarded steps' MLflow runs `openweight.superseded=true`.
4. Capture the JSON event stream to `runs/<branch>/step-NN.jsonl` and log it as an MLflow artifact.
   OpenCode's stream lacks the session totals (the last model call never gets its `step_finish`,
   and subagents run in child sessions whose events are not in it), so after each OpenCode session,
   timed-out ones included, the runner runs `opencode session export` on the session and,
   recursively, on every subagent session it started, and appends the exports to the same file as
   `openweight.session` events. Tokens, cost and tool calls come from those exports (all sessions);
   `num_turns` counts the top-level session's model calls, as Claude Code's does. The export's
   outcome also decides whether the session succeeded: on the first step-1 run (2026-10-30)
   `opencode run` 2.0.23 exited 1, with nothing on stderr or in its log, after a session it
   recorded as `succeeded`, which cost a needless retry. Without an export the exit code decides.
5. If the agent left uncommitted changes, the runner commits them itself
   (`chore(eval): uncommitted changes from step N`) and records `agent_committed=false`.
   If the agent made no commits at all, an empty commit with the trailers is created so the step
   is still recorded.
6. Run the objective checks (below) on the work as the agent left it, then record and tag the
   step, log results, and move on.
7. **One retry on failure.** A step fails when the harness errors or times out, or when a check
   fails that did not already fail after the previous step (so a check broken since step 5 does
   not trigger a retry at every later step). Which checks failed after the previous step is read
   from the previous step's MLflow run (its `Eval-Run-Id`); a check skipped there, or a step
   without a run, counts as not failing. The agent then gets exactly one retry: a fresh
   session, consistent with fresh-session-per-step, started on the workspace as the first attempt
   left it (its commits and uncommitted changes are kept, not reset). The retry prompt is the
   step prompt plus what went wrong (the harness error or timeout, or each newly failing check's
   command and the last 60 lines of its output) and asks the agent to finish the step and fix it.
   Checks run on the first attempt only if its session succeeded (a failed session is retried
   anyway), and again after the retry. Both attempts' event streams, check logs and traces are
   attached to the step run; tokens, cost, turns and duration sum both attempts; `retried` (0/1)
   records whether it happened; `status` and the check metrics reflect the final attempt.
   Usage-limit reruns are separate and do not consume the retry; a usage limit hit during the
   retry restores the workspace to where the first attempt left it. `--no-retry` turns the retry
   off.
8. If the step still fails after the retry: record `status=failed` (a session that succeeded but
   left newly failing checks is `failed` too, with `harness_status=ok`), commit what exists, and
   continue or stop according to `--on-failure {continue,stop}` (default `continue`, so later
   steps still get a score even when one step went badly).

## Objective checks (`checks.py`)

Run inside the evaluation's agent container after each step (same toolchain and same dind daemon
the agent used); each result is a 0/1 metric plus captured logs. Checks are
skipped (not failed) until the step that introduces the relevant tooling.

| Check | Command | From step |
|-------|---------|-----------|
| web install | `npm ci` (in `web/`) | 1 |
| web typecheck | `npm run check` | 1 |
| web lint | `npm run lint` | 1 |
| web build | `npm run build` | 1 |
| db push + seed | `npm run db:push && npm run db:seed` against a throwaway Postgres | 15 |
| compose config | `docker compose config --quiet` | 16 |
| dagster install | `uv sync` (in `isgs_dagster/`) | 21 |
| dagster defs load | `uv run dg check defs` | 21 |
| dagster tests | `uv run pytest` | 23 |
| docker build | `docker build .`; `docker build --target runtime .` from 16 (opt-in, slow) | 14 |

- **On a copy.** Checks never run in `/workspace`: a failed `npm ci` there would leave the next
  session without `node_modules` (the first trial run did exactly that). The runner copies the
  working tree as the agent left it to `/tmp/openweight-checks` in the agent container, with its
  uncommitted changes and ignored files (`.env`, `data/`) but without `.git`, the Access databases,
  `node_modules` and `.venv`, runs the checks there, and removes the copy. The install checks
  therefore install from the lock files as a fresh clone would. Checks run before the step is
  recorded, so a retry starts from the agent's state, not the runner's commit.
- **Environment.** Only `CI=true` is set. Nothing from PromptDB's conventions is injected: the
  conventions' Postgres `DATABASE_URL` would be a file path to the SQLite-era app of steps 1–14.
  Whatever else the app needs has to come from the agent's own (copied) `.env` files.
- **Throwaway Postgres.** The seed check starts `postgres:16-alpine` (as `ow-check-postgres`, in
  the evaluation's dind) on port 55432, so it never touches the agent's compose database on 5432,
  waits for `pg_isready`, runs push and seed with `DATABASE_URL` pointing at it, and removes it.
  Seeding reads the NDJSON extracts the agent's step-1 exporter wrote into `data/`.
- **Prerequisites.** A check whose prerequisite failed (the typecheck, lint, build and seed checks
  need web install; dagster defs and tests need dagster install) fails without running, and so
  does a check whose directory does not exist once its step has come.
- **Timeouts.** 10 minutes per check (20 for the seed and dagster tests, 30 for docker build).
- **Docker build** has no named stages before step 16, so it builds the default stage until then.
  It is opt-in: `openweight run --docker-build`.
- `openweight run --no-checks` turns checks off; `openweight check --harness ... --model ...
  [--step N]` runs them on a workspace as it is, without MLflow, logging to
  `runs/<branch>/check.checks/`.

Logged per step: `check.<name>` (1 passed, 0 failed; absent when skipped), `checks_passed`,
`checks_failed`, `checks_duration_s`, and the logs as `step-NN.checks/<name>.log` with a
`checks.json` summary (`step-NN.retry.checks/` for the retry's). Also logged: files changed, lines
added/removed, number of commits by the agent, and the share of the agent's commits (those the
runner did not author) with a conventional-commit subject (`commits_conventional`) and with the
`Assisted-by: <harness>:<model>` trailer PromptDB asks for (`commits_assisted_by`).

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
- **Parent run** per branch and PromptDB revision (reused across invocations, named after the
  branch): tags `openweight.role=branch`, `harness`, `model`, `effort`, `branch`,
  `harness_version`, `promptdb_sha`.
- **Child run** per step (`step-NN`, tag `openweight.role=step`): params `step`, `title`, `phase`;
  metrics `duration_s`, `cost_usd`, `input_tokens`, `output_tokens`, `cache_read_tokens`,
  `cache_write_tokens`, `num_turns`, `tool_calls`, `agent_commits`, `agent_committed`,
  `rate_limited_retries`, `discarded_cost_usd`, `retried`, `traces`, check results, commit
  conventions, diff stats
  (`files_changed`, `lines_added`, `lines_removed`); tags `status`, `harness_status`,
  `session_id`, `commit`, `error`; artifacts: `prompt.md`, the event stream, harness stderr, check
  logs, `diff.patch` (`step-(N-1)..step-N`). `input_tokens`
  excludes cached input, which is counted in the two cache metrics, as both providers report it.
- **Traces** (MLflow GenAI):
  - Claude Code: the `@mlflow/claude-code` plugin, which in MLflow 3.17 replaced the Python
    `mlflow autolog claude stop-hook`. `mlflow autolog claude` would install it from a GitHub
    marketplace at run time; instead the sandbox image pins the npm package and the runner loads it
    with `--plugin-dir`. Its Stop hook writes one trace per session; `claude.settings.json` sets
    `MLFLOW_CLAUDE_TRACING_ENABLED`, and the `MLFLOW_*` variables come from the environment.
  - OpenCode: the runner writes the trace itself, on the host, from the session exports (see
    Runner, step 4), because `@mlflow/opencode` does not load in OpenCode 2. The trace has the
    same shape as the plugin's (an `opencode_conversation` agent span with an `llm_call` span per
    model call, carrying its token usage, and a `tool_<name>` span per tool call), with each
    subagent session's spans nested under the tool call that started it. Because it is built from
    the export rather than a Stop hook, a timed-out session still gets a trace of what it did.
  - Neither harness's tracing can attach a trace to a given run, so after each step the runner
    finds the session's traces by their `mlflow.trace.session` metadata, links them to the step
    run (`link_traces_to_run`), and tags them `branch` and `step`. A Claude session killed by the
    timeout never reaches the Stop hook and has no trace.
  - Traces are for inspection, not metrics: on the first step-1 run (2026-10-30) the
    `@mlflow/claude-code` 0.4.0 trace had every tool call (32 TOOL spans) but only 12 of the
    session's LLM calls, and about a third of the output tokens Claude Code reported.
- Token/cost metrics come from the harness's own JSON output, so they are comparable even if
  tracing coverage differs between harnesses. `cost_usd` is **notional** for both harnesses: Claude
  runs on a subscription (the figure is Claude Code's API-equivalent estimate, not billed spend) and
  OpenCode computes Lumen cost from the per-token prices in `opencode.json`, which match what
  Lumen bills against the key's budget, cached input included. OpenCode reports
  reasoning tokens apart from output tokens; `output_tokens` includes them, as Claude's does
  (Lumen reported 0 reasoning tokens for every model on 2026-10-30, counting them as output).
  OpenCode's session totals include its title-generation call.

## Review (`openweight review`)

Run on request, separately from implementation. The unit of review is a **phase**: PromptDB's
seven phases (steps 1–13, 14–16, 17–20, 21–29, 30–35, 36–38, 39–42) are reviewed once each, when
every step in them has run, rather than step by step. A phase is a coherent piece of the app, so
the judge sees a change it can assess as a whole, and the cost is 7 judge sessions per branch
instead of 42.

```
uv run openweight review --branch eval/claude/claude-sonnet-5-5-high [--phases 1-7] [--judge claude|codex]
uv run openweight review --all            # every eval/* branch, for a leaderboard
```

- `--phases` defaults to every complete phase not yet reviewed; a phase whose last step has not
  run is refused.
- Input per phase: the phase's step prompts, the diff from the tag of the step before the phase to
  the tag of its last step (e.g. `step-13..step-16` for phase 2), each step's check results, and
  read access to the code at the phase's last tag (the judge runs as an agent with read-only tools
  so it can inspect files beyond the diff). The code comes from a read-only copy built from the
  main repository's tags, never from the live workspace.
- The judge also gets the agent's commit messages in the phase, which the Process criterion
  scores. It is **blind**: harness/model names are stripped from what it sees, and so are the
  `Assisted-by` and `Eval-*` trailers (whether the agent wrote `Assisted-by` is already measured
  by `commits_assisted_by`).
- Rubric (each 1–5 with a written rationale), applied to the phase as a whole:
  - **Completeness** — every requirement in the phase's step prompts is implemented.
  - **Correctness** — the code would work; no obvious bugs; checks pass.
  - **Code quality** — idiomatic Svelte 5 / Drizzle / Dagster, readable, no dead code.
  - **Consistency** — fits the existing codebase; no regressions of earlier phases.
  - **Scope discipline** — no unrequested features, no reproduced reverted history.
  - **Process** — commit messages, tests added where asked.
- Each phase review is a child run of the branch's parent run (`phase-N-review`, tags
  `openweight.role=review`, `phase`, `first_step`, `last_step`, `commit` of the last step's tag,
  `judge`), with the scores as metrics (`review.<criterion>`) and the rationales as an artifact.
  The judge session's trace is linked to it and carries the scores as MLflow
  **assessments/feedback**, with the judge model recorded as the source.
- Rerunning a step with `--reset` tags the reviews of its phase and every later phase
  `openweight.superseded=true`, as it does the step runs, since the code they judged is gone.
- Two judges from different vendors review every phase, to offset self-preference bias (Claude
  judging Claude); `--judge` limits a run to one. Each phase review gets one child run per judge,
  and `report` shows both judges' scores and their mean. Neither can be an `mlflow.genai` LLM judge
  calling an API directly, since there are only subscriptions (no API keys). Both run as headless
  agents with read-only tools in the read-only copy at the phase's last tag, return JSON scores
  against the rubric, and the runner writes them with `mlflow.log_feedback`:
  - **Claude:** `claude -p --effort high`.
  - **Codex:** `codex exec -s read-only -C <code> -m gpt-6.1-sol -c model_reasoning_effort=high
    --output-schema <rubric schema> -o <result.json> --json`, on ChatGPT Plus. The schema must set
    `additionalProperties: false` and list every property as required. `--json` events report
    tokens but not the model or quota, so the runner reads those from the session file in
    `~/.codex/sessions/` (`model`, and `rate_limits.primary` / `.secondary` `used_percent` for the
    5-hour and weekly windows).
- Tested on 2026-11-01 with phase 1 of `eval/opencode/lumen--glm-5.3` (steps 1–13), with the same
  prompt and schema for both runs:

  | | `gpt-6-luna` (free plan) | `gpt-6.1-sol` (Plus) |
  |---|---|---|
  | Time | 36 s | 1 min 48 s |
  | Shell commands | 11 | 27 |
  | Input tokens (cached) | ~450k (~400k) | ~400k (~290k) |
  | Output tokens | 2.6k | 4.2k |
  | Quota used | 1% of 30 days | 3% of 5 hours, ~0% of the week |
  | Scores, in rubric order | 5/4/4/4/5/5 | 4/3/4/4/5/4 |
  | Issues reported | 1 | 7 |

  Sol read most source files in full and found six issues that check out in the code:
  - The ESLint major version is 10, where step 1 asked for 9.
  - `visits.by` and `reviewedBy` are plain text, where step 7 asked for foreign keys.
  - A blank `typeId` becomes `0` and passes validation.
  - The station form puts saved values back after a failed edit in place of values the user
    cleared.
  - Adding or editing a station needs JavaScript, although the commit message says it doesn't.
  - The README's `cd ../web` runs from the repository root.

  Sol also claimed the `db.ts` database path breaks after bundling. That is plausible but
  unverified. It missed the nullability bug Luna found. **Use `gpt-6.1-sol`.** Its scores are
  stricter and its rationales cite files and lines. Both models still skimmed the 170 KB diff
  (Sol read its file list and first 160 lines). The judge prompt should have it work through the
  diff file by file, or hand it the diff in pieces.
- A final whole-branch review scores the end state (architecture, overall app quality) once all
  steps are done, after phase 7's review.
- `openweight report` produces a comparison table (per pair: total cost, time, check pass rate,
  mean review scores, per-phase breakdown) from MLflow. Step metrics (cost, time, checks) roll up
  into the same phases, so each phase's review sits next to what it cost.

## Implementation milestones

1. **Skeleton** — `uv init`, `pyproject.toml` (mlflow, typer, python-dotenv, gitpython or plain
   `git` subprocess), `promptdb.py` with tests that it parses all 42 steps.
2. **Branches** — workspace repo + orphan branch creation, input copying, trailer parsing, step
   tags, fetching branches back into the main repo.
3. **Sandbox** — `sandbox/Dockerfile`, dind + agent container lifecycle, `sandbox shell`/`down`;
   verify compose-published Postgres is reachable on `localhost` from the agent
   (`pytest --docker` does, along with mounts, isolation from the host daemon, env and timeouts).
4. **Claude harness** — run step 1 end to end with Claude Code; MLflow run + trace visible on the
   server.
5. **OpenCode harness** — same for OpenCode; normalize its JSON events into the same
   `StepResult` (tokens, cost, turns).
6. **Checks** — objective checks with per-step gating, run in the sandbox; extend the one-retry
   failure policy (already in place for harness errors and timeouts) to failed checks.
7. **Full run** — one complete 42-step run per harness with a baseline model; fix runner
   robustness issues (timeouts, failed steps, resume).
8. **Review** — per-phase judge, rubric, MLflow assessments, `report` command.
9. **Matrix** — `matrix.toml` with the five pairs above; `openweight run --matrix [--parallel N]`.

## Open questions

To resolve before milestone 4.

1. **Repeat runs.** *Resolved:* one run per pair, no repeats; branch names have no run suffix.
2. **Session strategy.** *Resolved:* a fresh session per step (retries included).
3. **Failure policy.** *Resolved:* the agent retries once with the error or failing check output,
   then the runner continues (see Runner, step 7).
4. **MLflow server.** *Resolved:* the tracking server runs MLflow 3.17, which supports GenAI traces
   and assessments (≥ 3.4 needed); the client is pinned to `mlflow>=3.17.2` to match.
5. **Judge model and cadence.** *Resolved:* one review per phase, plus a final whole-branch
   review, each by two judges from different vendors: Claude Code and Codex (see Review).
6. **Budget.** *Partly resolved:* Lumen's coin budget caps OpenCode spend, and the runner waits
   for it (see Runner, step 3). Claude runs on a subscription. Still open: per-run time caps.
7. **Remote.** *Resolved:* branches stay local and are never pushed.
8. **Step 43.** *Resolved:* excluded; evaluations run steps 1–42.
