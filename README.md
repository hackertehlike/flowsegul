# flowsegul

Turn a Python / FastAPI repo into an **interactive call-flow explorer** — a draggable node graph
where every endpoint fans out into the functions it calls, with inline source, color-coded data
flow, and git-diff change detection. One self-contained HTML file, opened in your browser.

Great for reviewing a PR without reading files in linear order: you see *how a request actually
flows* through controller → service → repository, which variable becomes which, and exactly what
changed on the branch.

## Install

```sh
git clone git@github.com:hackertehlike/flowsegul.git
cd flowsegul
./install.sh          # puts `flowsegul` on your PATH (~/.local/bin)
./install.sh --skill  # ...and links it as a Claude Code skill (optional)
```

Requirements: **Python 3.6+** and **git**. No pip install — pure standard library.

## Use

Run it from anywhere inside (or at the umbrella of) your repo(s):

```sh
flowsegul            # every FastAPI route across the repo, opens in your browser
flowsegul --changed  # ONLY what changed on the current branch vs main  ← the PR view
```

The `--changed` view guarantees **nothing in the diff is invisible**: every changed function is a
node in some endpoint's flow (or its own root chart if no route reaches it), and every changed
class/model shows as a chip or its own diff node.

### Common flags

| Flag | What it does |
|------|--------------|
| `--changed` | Restrict to what changed vs the base (`main` by default). The PR view. |
| `--base REF` | Change the diff base (e.g. `--base develop`). |
| `--branch REF` | Inspect any branch/ref **without checking it out** (reads via `git show`). |
| `--from REF [--to REF]` | Diff two commits/refs by hash directly (exact range). |
| `--entries fn1 fn2` | Specific functions instead of all routes. |
| `--repo DIR` / `--workspace DIR` | Point at specific repo(s) instead of auto-discovery. |
| `--out FILE` | Write to a specific path (default: a temp file). |
| `--depth N` | Call-graph recursion depth (default 6). |

Run `flowsegul --help` for the full list.

## What you get

- **Draggable node graph** per endpoint — scroll to zoom, drag to pan.
- **Inline source** per node, syntax-highlighted, shown by default.
- **Color-coded data flow** — each produced variable gets one consistent color across its chip, its
  arrows, and every line of source that touches it. Solid arrows = arguments in; dashed = value
  returned (labeled `type → variable`).
- **Change detection** (`--changed`) — per-node Diff / New / Old toggle, a `↕ Changes` navigator,
  and deleted defs shown as removal diffs.
- **Review mode** — tick nodes as reviewed (persisted in localStorage).

## How it works

`scripts/flowsegul_gen.py` AST-parses your Python, builds the call graph, and injects it as JSON
into `scripts/template.html` (a generic, self-contained viewer). No per-PR hand-authoring — it's
all derived from the source.

## Claude Code skill

If you use [Claude Code](https://claude.com/claude-code), `./install.sh --skill` links flowsegul as
a skill. You can then ask Claude to "visualize the call flow for this PR" — and, more usefully, have
it **generate the graph and read it back to you for bugs** (variables produced but never consumed,
skipped layers, contract mismatches).

## Roadmap

- **FE consumers + contract linter** — cross the Python→TS boundary: for each endpoint, show the
  frontend call sites that consume it and flag BE-only / FE-only / type-mismatch fields.
