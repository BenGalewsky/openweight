# Build a pressureTemperatureDepth review panel for excluding report rows

## Context
The pressureTemperatureDepth table has  roughly 4,000 rows for any given stationVisit. 
Before generating a final report, users need to review those rows and mark any they want excluded from that report.

## Goal
Add a full-screen review panel (not a small centered modal — it needs room for a large data table) where 
users can browse all rows, search/filter to find specific ones, and toggle individual rows in or out of 
the report. Default state: every row is included; users mark exceptions.

## Requirements

### Layout
- Full-screen overlay or large slide-out drawer, opened from vists/[id]/station-visits/[svid] page. Add a "Review PTD" button in the "PTD Measurements" section — consider Svelte's built-in transition directives (`fly`, `fade`, etc.) for the open/close animation
- Header with title and close button
- Include filter controls for:
  - Time Range
  - Temperature less than a provided value (default to 0 celsius)
  - Depth less than a provided values (default to 0)
- Bulk action bar: "Exclude all matching current filter" and "Include all matching current filter"
- Scrollable, virtualized table of all rows
- Footer with a live count ("X of 4,000 rows will be included in the report") and Cancel / Save buttons

### Table behavior
- One column is a checkbox/toggle: checked = included in the report (default checked for every row)
- Excluded rows stay visible but are visually de-emphasized (reduced opacity, strikethrough text)
- The table is virtualized so only visible rows are rendered — must stay performant with ~4,000 rows
- Filters narrow the visible set without discarding exclusion choices made elsewhere

### State management
- Track excluded row IDs as a set, independent of the current search/filter state — a Svelte writable store holding a `Set<id>` works well, since the live count, bulk-action buttons, and row checkboxes all need to read and react to it
- Derive the "X of 4,000 included" count and the filtered row list with `derived` stores (or `$:` reactive statements) rather than recomputing manually on each change
- batch all changes until "Save" is clicked
- On Save, persist the exclusion set by updating the new boolean column `includeInReport` in the pressureTemperatureDepth table

## Before you start
- Explore the existing codebase to identify the SvelteKit version and conventions in use: routing structure, how page/load data is fetched, any component library already adopted (e.g. Skeleton, Flowbite-Svelte, bits-ui/shadcn-svelte, Melt UI), and the styling approach (Tailwind, UnoCSS, scoped component styles)
- Check whether a virtualization library is already in use (`@tanstack/svelte-virtual`, `svelte-virtual-list`, `svelte-tiny-virtual-list`, etc.); use it if present, otherwise pick one that fits the existing stack
- Decide whether the review panel should be its own route or an overlay component toggled via a store/context — match whatever pattern the app already uses for similar full-screen UI
- Ask me about anything unclear regarding the data schema, API endpoints, or where this panel should be triggered from before writing code