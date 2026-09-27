---
name: flowsegul
description: Generate an interactive HTML call-flow explorer for a set of Python endpoints/functions — a draggable node graph showing every method called, with inline source code, color-coded data flow (each variable one color across chips, arrows, and code), and labeled forward (args) / return (typed value) arrows. Use when the user wants to understand or review the flow of new endpoints/functions in a PR without reading files in linear order (e.g. "visualize the call flow for this PR", "show me how these endpoints work", "make the flowsegul thing for X"). Works on any Python repo with the AST generator; no per-PR hand-authoring needed.
---

# flowsegul

Reusable tool that turns Python entry functions into an interactive call-flow explorer.
`scripts/flowsegul_gen.py` AST-derives the whole graph; `scripts/template.html` is the generic
self-contained viewer. **Do not rebuild the visualizer from scratch — run the generator.**

## What it produces

A single self-contained HTML file: a per-endpoint draggable node graph where each node is a
function (controller / service / repository / helper layer, color-chipped). Per node it shows the
data-flow steps (`var = call(args)`) and, on demand, the real source (syntax-highlighted; hidden by
default for an overview, `C` shows all; changed functions always show their diff). Arrows: **solid** = arguments sent into a call; **dashed** = value returned (labeled
`type → variable`). Every produced variable gets one consistent color across its chip, its arrows,
and every line of source that touches it; request inputs get fixed colors (with repo-layer aliases,
e.g. `start_date`→`start`).

## Run it — zero config

From anywhere inside (or at the umbrella of) the repos:

```sh
flowsegul            # every FastAPI route across the Python repos, opens in browser
flowsegul --changed  # ONLY what changed on the current branch(es) vs main  ← use this for a PR
```

`flowsegul` is `scripts/flowsegul` symlinked onto your PATH (via `install.sh` → `~/.local/bin`),
which runs the generator with smart defaults and `open`s the result. When **Claude** does it (chat trigger), skip the alias and
run the generator directly to a local file — **never the Artifact tool** (the user delivers this as
local HTML). The point of Claude running it is to then **read the graph and review the flow for
bugs** (vars produced-but-unused, contract mismatches, skipped layers), not to ship it off:

```sh
python3 scripts/flowsegul_gen.py --changed --out /tmp/flowsegul.html   # then open it / read the graph
```

**Defaults (all overridable):**
- **Repos** — auto-discovered: the umbrella dir's git subdirs that contain Python (`src/`), or the
  current repo + its siblings. Override with `--workspace DIR` or repeatable `--repo DIR`.
- **Files** — each repo's `src/` (or `app`/`lib`, else repo root) is walked; tests, `alembic`,
  `migrations`, venvs excluded. Override with `--files`.
- **Entries** — every `@router.<method>("/path")` function (FastAPI). Or `--entries fn1 fn2`.
- **`--changed`** — restrict entries to files changed vs `--base` (default `main`). Filters by
  **file** (a touched controller ⇒ all its routes), and for the current checkout also counts
  staged/unstaged edits. This is the PR view.
- **`--base REF`** — the diff base for `--changed` (e.g. `--base develop`, `--base master`).
- **`--branch REF`** — inspect any branch/ref **without checking it out**; source is read via
  `git show REF:path`, and the file list comes from that ref (so branch-only new files are
  included). Combine with `--changed` to see just what that branch changed vs `--base`, e.g.
  `flowsegul --branch origin/feat/x --changed`.
- **`--from REF [--to REF]`** — diff **two commits/refs by hash directly** (exact `FROM..TO`, no
  merge-base), implies `--changed`. `--to` defaults to the working tree. E.g.
  `flowsegul --from a1b2c3 --to d4e5f6` or `flowsegul --from HEAD~5`. Use this when you want a
  specific commit range rather than the branch-vs-base view. (`--base`/`--branch` also accept raw
  hashes, but they diff via the *merge-base* — branch semantics.)
- **`--depth`** (default 6), **`--title`**, **`--out`** (default a temp file).
- **`--react [DIR]`** — map a React + TypeScript app (or only the folder `DIR` in the repo): each
  user action (click, typing, submit, page load) and the ordered path it sets off — handler,
  callback props up the tree, the `useState` setter, the components that rerun, effects on that
  state and the requests they make. On by default for a repo with `.tsx` files. With a FastAPI
  backend too, each request links to the route it reaches, the route lists its callers, and red
  marks flag `no route`, `GET ≠ POST`, a route declared earlier that catches it, and query keys
  the route ignores or requires.
  Needs `node` and the `typescript` package (the app's own, a global one, or `FLOWSEGUL_TS=DIR`).
  Reads the working tree; `--changed` etc. apply to routes only. Anything it can't pin down is
  marked uncertain (faded), never claimed.

**Coverage guarantee (`--changed` / `--from`):** every changed *function* is drawn as a node in
some endpoint's flow, or as its own root chart if no endpoint reaches it; every changed *class*
(schema/ORM model) shows as a chip, or — for pure class-level changes like an added column or a
removed method — as its own diff node. So nothing in the diff is invisible.

Multi-repo PRs produce **one combined HTML**, endpoints grouped by repo in the sidebar (cross-repo
calls are HTTP, so each repo stays its own graph). Calls resolve by name within each repo, so
`db_access.message.get_x` → `MessageRepository.get_x`; anything not indexed (stdlib, ORM) is a leaf.

## Deliver it — local HTML, not Artifact

The output is a self-contained page. Deliver it as the local `.out` file the user opens in a
browser (`open /tmp/flowsegul.html`, or they run `flowsegul`). **Do not use the Artifact tool** — the
user doesn't want it. Verify first by loading the HTML (jsdom or a browser) and checking `#edges
path` count and `.node` count are non-zero before reporting results.

## Notes / limits

- Auto labels are literal: return labels use the callee's declared return annotation; arg labels
  use the source arg expressions. Fine for review; hand-curation (shorter type names, prose
  summaries) is optional polish, not required.
- Input-param aliases across renames (`start_date`→`start`) are inferred only when names match; if a
  value is renamed to something unrelated the color won't follow it.
- Side-effect calls (no assignment, e.g. `await db.commit()` that resolves in-scope) show as a `·`
  row.
- The template is generic: `graph.endpoints[].nodes[].steps[]` drives everything. You can also
  hand-write that JSON and inject it into `template.html` (replace `__DATA__`) if you want full
  control instead of the generator.
