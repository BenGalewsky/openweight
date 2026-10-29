PLAN.md — Regenerating the ISGS/IDOT Wetlands Water Database
This plan rebuilds the repository step by step, following the commit history (459fbc1 → 2756ea7, 2026-05-14 → 2026-08-24). Each step is written as a prompt you can hand to a coding agent. Run them in order; each assumes the previous steps are done and committed.

Conventions for every step (from CLAUDE.md):

Commit with conventional commits and add the trailer Assisted-by: <harness>:<model>.
Dev DB is the docker-compose Postgres: run npm run db:push with DATABASE_URL=postgresql://idot:idot@localhost:5432/idot.
Use the Svelte MCP tools (list-sections, get-documentation, svelte-autofixer) whenever writing Svelte code.
Repo layout the steps converge on:

IDOT.accdb, Wetlands_IDOT_BE.accdb   # source Access databases (inputs)
data/                                # NDJSON seed exports + sample logger files (gitignored)
plans/                               # design docs (access_db_schema.sql, ptd_review_window.md, ...)
scripts/                             # one-off Python helpers (Access export)
scratch/                             # dagster CSV output (gitignored)
web/                                 # SvelteKit 3 / Svelte 5 / Tailwind 4 / Drizzle app
isgs_dagster/                        # Dagster 1.13 ingestion project (uv, Python)
Dockerfile, docker-compose.yml       # web image (builder/seeder/runtime) + local stack
.github/workflows/release-docker.yml
Reverted history (do not reproduce): f624a8f lab-results Dagster asset, 5a59785 lab-results import UI, and their reverts 2cf0f0d/9cc8713.

Phase 1 — Web app foundation (SQLite era)
Step 1 — Scaffold the SvelteKit wetlands app (459fbc1)
Create a SvelteKit 3.1 + Svelte 5 + TypeScript app in web/ named idot-wetlands, styled with Tailwind CSS v4 (@tailwindcss/vite, src/app.css imports tailwind). Use Drizzle ORM with a local SQLite file (better-sqlite3, drizzle.config.ts, src/lib/server/db.ts, src/lib/server/schema.ts) and scripts dev, build, check, db:push, db:studio, db:seed. Add ESLint 9 + Prettier (with svelte plugin; tabs, single quotes). The source data lives in IDOT.accdb / Wetlands_IDOT_BE.accdb (Microsoft Access). Add scripts/ with a small uv-managed Python project (pyproject.toml, .python-version pinned to 3.13, main.py) that exports Access tables to NDJSON files in data/ by calling the MDBTools command-line tools (mdb-tables, mdb-json) via subprocess. Do not use pyodbc: no MDBTools ODBC driver is installed. Define a first projects table (ISGS number, IDOT name, ISGS name, begin/end dates, FA number, county, type, seq code) plus a LUT_SiteType lookup, a web/scripts/seed.ts that loads NDJSON from data/ idempotently (onConflictDoNothing, chunked inserts, a parseDate helper that converts MM/DD/YY to ISO), and a home page +page.server.ts / +page.svelte listing projects in a table. Add .gitignore, .dockerignore, web/.env.example, and a README.

Step 2 — University of Illinois branding (8c6e153)
Apply University of Illinois brand guidelines to the site: Illinois Blue (#13294B) and Orange (#FF5F05) as Tailwind theme tokens in app.css, brand fonts, a branded header/footer in routes/+layout.svelte, and consistent table/heading styling on the home page.

Step 3 — Document the Access DB (d44e676)
Dump the schema of Wetlands_IDOT_BE.accdb into plans/access_db_schema.sql (tables, columns, relationships) so future steps can reference it.

Step 4 — County names and active-project filter (ef6e4ab, 38daad2)
Add the LUT_County_Names table (cntycode PK, cntyname), seed it from data/LUT_County_Names.json, and join the county name into the home-page projects grid. Then filter the home page so projects whose end date is in the past are excluded.

Step 5 — License and README housekeeping (93aa2f1, 1c23b85, 91deb8f, 93e6494)
Add LICENSE.md, move the README to the repo root, document the Svelte Claude plugin / MCP setup in it, and refresh it to describe quick start, dev commands, and project structure.

Step 6 — Project detail page (c0afb80)
Add routes/projects/[id]/+page.server.ts and +page.svelte showing a project's fields, and link the IDOT name in the home table to it.

Step 7 — Initials lookup, nav menu, Field Scientists CRUD (502c41a, 0093329)
Add the lutc_initials table (initials PK, first/last name), seed from data/LUTC_Initials.json. Add a top navigation menu and Field Scientists CRUD pages (list, create, edit, delete, detail by initials) using SvelteKit form actions.

Step 8 — Visits (dc586b2, e952140, da315ce)
Add a visits table (project FK, date, by FK to initials, note, reviewed-by FK, reviewed date) and seed it from data/Visits.json (624 records). Show a visit-count column on the projects and field-scientist lists, and a visits table on the project and field-scientist detail pages.

Step 9 — Pre-commit hooks (d80f9c8)
Add husky + lint-staged in web/ (prepare script runs cd .. && husky web/.husky; hook runs lint-staged: prettier on ts/js/svelte/css/json/md, eslint --fix on ts/js/svelte). Reformat the codebase with Prettier.

Step 10 — Stop tracking seed data (b6abea8)
Remove data/ seed files from git and gitignore them; document in the README that NDJSON exports must be produced from the Access DB locally.

Step 11 — Stations schema + lookups (a12e97f, f534039)
Add the lookup tables lut_station_type, lut_location_type, lut_station_inst_type, lut_station_units, lut_station_read_type, lut_boring_method, lut_status, and the stations table (site FK, type FK, code, dates, name, alt label, lon/lat, location type, initials, instrument type/units, read type, ISGS id, boring date/method, comment). Extend seed.ts to load all of them plus Stations.json. Add a stations section with full CRUD (modal forms, form actions) to the project detail page.

Step 12 — Sortable projects table (b0f2118)
Add client-side column sorting (click header, asc/desc indicator) to the projects table on the home page.

Step 13 — Move people routes under maintenance (468876e)
Reorganize routes: field scientist pages move to routes/maintenance/people/ and routes/maintenance/people/[initials]/ with a maintenance/+layout.svelte; update nav links.

Phase 2 — Containers and Postgres
Step 14 — Dockerfile (fd92148)
Add a multi-stage Dockerfile (node:22-alpine) that builds the SvelteKit app with @sveltejs/adapter-node and a .dockerignore.

Step 15 — Migrate SQLite → PostgreSQL (ce9fc9c)
Migrate Drizzle from better-sqlite3 to Postgres (postgres driver, drizzle-orm/pg-core, drizzle-orm/postgres-js). Convert schema.ts to pgTable (serial PKs, doublePrecision, etc.), read DATABASE_URL in db.ts, drizzle.config.ts, and seed.ts, make all route loaders async against Postgres, remove the committed dev.db* files and SQLite deps, and add DATABASE_URL to .env.example.

Step 16 — docker-compose workflow (ccd39a9)
Add docker-compose.yml with postgres (16-alpine, user/db/password idot, healthcheck, host port ${POSTGRES_HOST_PORT:-5432}), a seed service (Dockerfile target seeder: installs deps + postgresql-client, runs npm run db:push && npm run db:seed, mounts ./data:/data), and a web service (target runtime, port 3000, ORIGIN env — adapter-node CSRF check needs it — depends on seed completing). Split the Dockerfile into builder, seeder, runtime stages. Update the README.

Phase 3 — Visits, station visits, uploads
Step 17 — Visits section (5228c92)
Add top-level /visits (list of all visits with site, date, scientist) and /visits/[id] (detail) pages, and integrate visit creation/links into the project detail page.

Step 18 — station_visits table (9066e37, 4e1900d, e2c7b66)
Add the station_visits table (visit FK, station FK, status FK to lut_status, level_meters numeric(10,3), level_feet numeric(10,2), time, notes), seeded lut_status. When creating a visit, also create station-visit rows for chosen stations; show them on the visit detail page. Then add inline editing of a station visit through a modal dialog.

Step 19 — RustFS object storage + file upload (9bd2797)
Add a rustfs (S3-compatible) service to docker-compose (ports 9000/9001, RUSTFS_* credentials with defaults, healthcheck). Add web/src/lib/server/storage.ts using @aws-sdk/client-s3 (forcePathStyle, ensureBucket, uploadFile). Add a file-upload control to the station-visit form on /visits/[id] that uploads to S3. Document RustFS in the README.

Step 20 — Import queue (a49cb50)
Add an import_queue table (id, station_visit_id FK, uri, timestamp default now) and insert a row whenever an uploaded file is stored for a station visit.

Phase 4 — Dagster ingestion
Step 21 — isgs_dagster project (602e725, 9f837aa)
Create isgs_dagster/ as a uv-managed Dagster 1.13.6 project (hatchling, [tool.dg] root_module isgs_dagster, .python-version pinned to 3.13 — the default python3 here is 3.15, which lacks wheels for some Dagster dependencies — .env.example, README). Add resources.py with PostgresResource (psycopg2, DATABASE_URL via dg.EnvVar) and ObjectStoreResource (boto3, S3_ENDPOINT, S3_ACCESS_KEY, S3_SECRET_KEY), definitions.py, and a stub data_logger asset. Add the seeder's CREATE DATABASE dagster step (a second DB reserved for Dagster). Then add a sensor (station_visit_import_queue_sensor, 30 s interval, cursor on the queue timestamp, one RunRequest per new row with run_key=str(row_id)) and an imported_file asset job that runs data_logger.

Step 22 — PTD table and CRUD UI (c82cce0)
Add a pressure_temperature_depth table (station_visit_id FK, pressure, temperature, depth as real) and inline CRUD UI for those rows on the station-visit view.

Step 23 — PTD CSV ingestion (6b1cc52, 2937aef, 63cc2b4)
Replace the data_logger stub with an asset that downloads the S3 CSV (s3://bucket/key), parses the logger export, and bulk-inserts into pressure_temperature_depth. Rename ptd_logger → data_logger and make re-imports idempotent (delete existing rows for the station visit first). Add Diver-format support (read_type/station_type config) and a temperatures table (mirror in schema.ts, surface on the visit page). Add pytest tests in isgs_dagster/tests/test_data_logger.py using fixtures like data/DIVER_49_5L_GW_20241002.CSV.

Step 24 — Station visit detail page (b3c5d7f)
Move station-visit detail to its own page visits/[id]/station-visits/[svId] (PTD table/charts, edit) and simplify the visit page.

Step 25 — Samples (2ae9c43)
Add a samples table and inline CRUD UI for samples on the station-visit page.

Step 26 — Rename import queue; time-series charts (0ec27d3, e873a5f)
Rename import_queue → station_visit_import_queue (update schema, web code, and Dagster sensor SQL); a visit_labs_import_queue was added in that commit and removed later in 7ccf6d1 — skip it. Add a timestamp column to pressure_temperature_depth, populate it from the logger files, and replace the profile charts with chart.js time-series plots.

Step 27 — Import station visits from Access (5b49984)
Extend web/scripts/seed.ts to import station visits from the Access export (data/SO_Read.json) idempotently.

Step 28 — PTD review panel (ee71555, 7ccf6d1)
Implement plans/ptd_review_window.md: add include_in_report boolean not null default true to pressure_temperature_depth, a full-screen PtdReviewPanel.svelte with time-range / temperature / depth filters, bulk include/exclude on the filtered set, a virtualized table (@tanstack/svelte-virtual), live "X of N rows included" footer, and Cancel/Save wired to a form action. Remove the unused visitLabsImportQueue schema definition.

Step 29 — Sample detail page + sonde pipeline (656d71a)
Add sonde_import_queue and sonde_data tables (YSI/VuSitu low-flow columns: conductivity, salinity, turbidity, pH, ORP, RDO, temperature, battery, barometric pressure, …) and extend samples with pump type, flow rates, tubing type, device model/serial. Add the per-sample page visits/[id]/station-visits/[svId]/samples/[sampleId] (metadata, file upload to S3 + queue row, time-series charts). In Dagster add defs/sonde_data.py (parse_vusitu, sonde_data asset populating sample metadata and rows) and sonde_import_queue_sensor.py with the sonde_imported_file job; register them in definitions.py. Sample fixture: data/VuSitu_Low-Flow_*.csv.

Phase 5 — UI refactor and domain changes
Step 30 — S3 env var rename (d308a28)
Rename RUSTFS_* app env vars to generic S3_ENDPOINT, S3_ACCESS_KEY, S3_SECRET_KEY; require them at startup; update compose, README, .env.example, and the Dagster resource.

Step 31 — bits-ui and shared components (637ddfe, 8e02bd6, 0ea6665, 1f59c0a)
Migrate the UI to bits-ui. Create src/lib/components/ (AppDialog, Button, SelectField, TextField, TextareaField, TableHeader) and src/lib/forms.ts; rewrite all pages to use them. Generate the initial Drizzle migration (web/drizzle/0000_*.sql + meta). Extract src/lib/format.ts formatDate and use it for visit dates everywhere. Fix the modal close behaviour.

Step 32 — projects → sites; prefer ISGS name (fb506c1, 7f09733)
Rename "projects" to "sites" throughout the web app: table sites, routes sites/[id], siteId FKs, labels. Then prefer the ISGS name over the IDOT name across the UI. (Update seed.ts to map Projects.json to sites.)

Step 33 — Housekeeping and form validation (632f9b5, b696d65)
Untrack dev.db and gitignore any local SQLite DB. Validate time and conditional status on the station-visit form (visitHelpers.ts: status required/forbidden depending on whether a reading was taken, time format).

Step 34 — Level by units (3359d45)
Store station-visit level in meters or feet depending on the station's instrument units (lut_station_units); compute the other value with the unit conversion factor and show the correct one in the UI.

Step 35 — Samples as bottles (52ded7a)
Re-model samples as bottles allocated to a visit: samples.visit_id (required), station_visit_id optional, sample_name varchar(32). Add a samples/bottle allocation section on visits/[id] and update the station-visit loader/page to attach existing bottles. See plans/samples-redesign.md for intent.

Phase 6 — More loggers and review polish
Step 36 — Aqua TROLL ingestion (03292e8)
Rework data_logger to parse In-Situ Aqua TROLL 200 WinSitu CSV exports (long metadata preamble; data header is Date and Time, Seconds, Temperature (C), Pressure (PSI), Depth (m), Specific Conductivity (uS/cm), Barometric Pressure (PSI)) straight into pressure_temperature_depth (add specific_conductivity and barometric_pressure columns; drop the separate temperatures table and UI). Update tests (fixture: data/INS_49_SW43_SW_20250924.csv) and the web station-visit page/loader accordingly.

Step 37 — Plot filtering and BARO stations (bad6662, 05ed261, fba6645, 19988ae)
Omit include_in_report=false points in the plots. For BARO stations loaded with the In-Situ logger, load barometric pressure into barometric_pressure (extend data_logger.py, the sensor config that passes station_type/read_type, and tests). Show barometric pressure instead of depth for BAR stations in the page and PtdReviewPanel. Add column sort controls to the review panel.

Step 38 — Graphing-data workbooks from Taiga (6814cf6)
Add defs/processed_graph_files.py: a TaigaResource (S3-compatible, TAIGA_ENDPOINT/ACCESS_KEY/SECRET_KEY), a dynamic-partitioned asset processed_graph_files (partition = date-range folder like 20230901_20241002 in bucket graphing-data) that reads each .xlsx, melts the WLE_GW and DTW_GW sheets to long form, joins them, adds a water_table_threshold flag (depth ≥ 0.3048 m), and writes scratch/<folder>_<file>_wle_gw.csv. Include a retry policy, per-file failure collection, and a discover_s3_folders sensor that registers new partitions (registers only, no auto-run, if >25 are new). Add pandas and openpyxl, register everything in definitions.py, write tests/test_processed_graph_files.py, gitignore scratch/.

Phase 7 — CI and deployment
Step 39 — Web image workflow (b874e80)
Add .github/workflows/release-docker.yml: on release published, build the web image for linux/amd64 and linux/arm64 (native runners), push by digest to ghcr.io/prairieresearchinstitute/isgs_waterdb, then a merge job creates the multi-arch manifest tagged with the release tag.

Step 40 — Lazy db/S3 init (c1ab937)
Make vite build succeed without env: db.ts exports a Proxy that connects on first use, and storage.ts builds the S3 client lazily and validates env inside s3(). Importing either module must be side-effect free (SvelteKit's postbuild analyse imports every server module).

Step 41 — Seeder image (d0c30ef)
Extend the workflow matrix to publish a second image (...isgs_waterdb-seed, Dockerfile target seeder) for both architectures, keeping variant unique per matrix row. Note in comments that data/ is gitignored and must be mounted at /data.

Step 42 — Containerize Dagster (2756ea7)
Add isgs_dagster/Dockerfile (uv, uv sync --locked --no-default-groups --group deploy; final stage named dagster; one image for webserver, daemon, code location, and run containers, role chosen via command; ISGS_SCRATCH_DIR=/scratch), .dockerignore, .python-version, and deploy dependency group (dagster-webserver, dagster-postgres, dagster-docker). Make SCRATCH_DIR in processed_graph_files.py configurable via ISGS_SCRATCH_DIR. Add deploy/DEPLOYMENT_PROMPT.md describing the production deployment, expand the Dagster README, and add the dagster variant (context ./isgs_dagster) to the release workflow.

Not yet committed (working tree at time of writing)
These exist as uncommitted changes and are intentionally outside the history-matching steps above; treat them as a follow-up step 43 if desired:

isgs_dagster/dagster.yaml, deploy/dagster.yaml.example, deploy/docker-compose.dagster.example.yml, deploy/workspace.yaml.example
edits to docker-compose.yml, isgs_dagster/README.md, .gitignore, .dockerignore, DEPLOYMENT_PROMPT.md, data_logger.py
design notes in plans/ (samples-redesign.md, ptd_review_window.md, web_code_quality_review.md, ProjectDataHousingFilePaths.docx)
Caveats
Several steps are reconstructed from commit titles and diffstats, not full diffs; an agent should consult git show <sha> for the cited commit when exact behaviour matters.
Seed inputs (data/*.json, sample logger files, .accdb files) are not reproducible from code; they come from the ISGS Access databases.