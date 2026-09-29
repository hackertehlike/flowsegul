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
flowsegul serve      # local server: pick branch / commit / range from inside the page
```

With `flowsegul serve`, the button at the top left of the page picks what to show: **Everything**
at a ref (or your working tree), **Branch changes** since a branch forked from a base (the PR view),
or a **Commit range** between two commits. It suggests branches, tags and recent commits with their
messages. The server only listens on `127.0.0.1`. A saved HTML file has the same button, and it
gives you the command to run for the range you pick.

The `--changed` view guarantees **nothing in the diff is invisible**: every changed function is a
node in some endpoint's flow (or its own root chart if no route reaches it), and every changed
class/model shows as a chip or its own diff node. Changed migrations (`alembic/`, `migrations/`) get
their own entry, with their `op.*` operations listed and the file diff.

### Code outside controller → service → repository

Each box is labelled by what kind of code it is, going by file and folder names:
controller, service, repository, dependency (`deps.py`), model, schema, factory, util, task, external (clients,
integrations), config and migration. Code that fits none of these is labelled with its folder name,
e.g. `domain` or `billing`. To name things your own way, add a `.flowsegul.json` at the repo root:

```json
{"layers": {"src/app/core/*": "domain", "src/app/legacy/*": "legacy"}}
```

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
| `--serve` | Serve locally instead of writing a file (`flowsegul serve`); `--port`, `--no-open`. |

Run `flowsegul --help` for the full list.

### React + TypeScript apps

```sh
flowsegul --react            # or just `flowsegul` in a repo with .tsx files
flowsegul --react frontend   # only this folder of the repo
```

The sidebar lists what a user can do, grouped by folder: `click Apply`, `type in coupon`,
`submit Log in`, and what a screen loads by itself as it opens (`load /orders`). Pick one and the map numbers the real code lines it runs through, in
order: the handler, the callback props it goes up through, the `useState` setter, the components
that rerun (a `reruns` chip; hover it for the line responsible), effects that depend on the state,
and the requests they make (`GET /orders/preview`). Step through with `N` / `Shift+N`; `Esc`
clears. Hover a `useState` line to see every place that sets it (`set in 2 places`), click to pin.
Click a component tag, a prop or a setter to jump to where it comes from; `Alt+←` goes back.

It needs Node.js and the `typescript` package: the app's own, one installed globally, or a folder
named by `FLOWSEGUL_TS`. Labels and paths come from the code by fixed rules. Anything that can't be
followed for sure (props passed on with `{...props}`, values from a context) is drawn faded as
uncertain.

When a click saves something and then refreshes a list (`queryClient.invalidateQueries(...)`), the
path goes on to the component showing that list and the request it sends again (`GET /comments`).
With an action picked, its boxes line up left to right in the order the steps reach them.

**State a diff erases.** With `--changed` / `--from`, a `useState` whose component the change
puts under a new condition (`{showPromo && <CouponField />}`), after an early return, or behind a
new key gets a red mark on its line, `draft erased when showPromo is false`: whatever the user
typed there now starts over each time the condition flips. Hover for the line responsible; a
`Problems` list at the top of the sidebar collects them.

### Frontend requests linked to their routes

A repo (or workspace) with both a FastAPI backend and a React app gets both maps, and each request
the frontend makes is matched to the route FastAPI would send it to. The request line carries the
route as the backend writes it (`GET /orders/{order_id}`); click it to open that route, `Alt+←` to
come back. A route's header lists who calls it (`called from PriceLine OrderPanel`), each a link to
the call, or says `no caller`.

Red marks say where a request won't work as written: `no route`, `GET ≠ POST`, `goes to
/orders/{order_id}` (a route declared earlier in the same file catches it), `no param n` (a query
key the route doesn't read, which FastAPI silently ignores) and `needs item_count` (a required
query key it doesn't send). A grey `307` is a missing or extra trailing slash; `route ?` is a URL
built at run time. Requests are read through `fetch`, axios (instances with a `baseURL` too),
`useSWR`, generated clients (hey-api, openapi-fetch, openapi-typescript-codegen) and your own
wrappers like `api('/items')`. Vite's `server.proxy` rewrites, Next.js `rewrites()` and `VITE_*` /
`NEXT_PUBLIC_*` values from `.env` files are applied.

## What you get

- **Draggable node graph** per endpoint — opens fitted to the screen; drag to pan, pinch or
  Ctrl/⌘+scroll to zoom, and a Pan / Zoom switch (`W`) for what the mouse wheel does; minimap to jump around.
  Code wraps instead of scrolling sideways. Click a call in the code to fly to its box; `Alt+←` goes back.
- **Find in map** (`Ctrl/⌘+F`) — finds function names and code lines in the boxes on screen;
  `Enter` / `Shift+Enter` step through the matches.
- **Inline source** per node, syntax-highlighted, one click (or `C` for all) away. Changed
  functions always show their diff.
- **Color-coded data flow** — each produced variable gets one consistent color across its chip, its
  arrows, and every line of source that touches it. Solid arrows = arguments in; dashed = value
  returned (labeled `type → variable`).
- **Values followed across calls** — a parameter takes the colour of what its caller passed, so
  `apply_coupon(subtotal)` shows its `total` parameter as the caller's `subtotal`. Each box lists
  what its parameters received (`total ← subtotal`) and which fields it reads (`payload` reads
  `.coupon`, `.items`). Hovering a value lights it wherever it goes, with parts of it (`payload.items`)
  outlined dashed. The request model's panel says which function reads each field, and which fields
  nothing reads.
- **Click a value to trace it** — clicking a variable numbers its path on the map: where it comes
  from (the route parameter, each call it's passed through, each return it lands in, local lines
  like `items = payload.items`), then where it goes next, up to the response. The rest of the map
  fades and a short list repeats the steps as code; click a step to go there, `Esc` to clear.
- **Ask a box** — the `?` on each box (or `?` on a focused box) asks where one of its parameters
  comes from, or why it can fail (each `raise` below it, up through the callers to the `except` that
  handles it or the status the client gets). The answer is drawn the same way. Clicking a box's
  `3 routes` tag lists the other routes that reach it; click one to open it.
- **Full route paths** — prefixes from `APIRouter(prefix=…)` and `include_router(…, prefix=…)`
  (also a constant or `settings.API_V1_STR`) are joined into the path. FastAPI dependencies
  (`Depends(get_current_user)`, `Annotated[…, Depends(…)]`) are drawn as calls.
- **Side effects in order** — a strip under the route header lists what the request does to the
  world, in the order it runs: `add Order` → `email` → `commit`. It covers database writes
  (`session.add`, `delete`, `execute(update(…))`), the commit (`session.commit()`, the end of
  `with session.begin():`, or a commit after `yield` in a Depends generator, drawn dashed and
  labelled `get_db`), and steps that can't be undone: email, HTTP POST/PUT/PATCH/DELETE, stripe,
  boto3 writes, files, queued tasks. A step that can't be undone gets a red squiggle when an error
  or the commit can still come after it; hover it to see which, and click any chip to go to its box.
  Steps on only some paths are faded, with their `if`. With writes but no commit in sight the strip
  ends in `commit ?` and marks nothing red. Name your own wrappers in `.flowsegul.json`:
  `{"irreversible": ["mailer.send_*", "*.instance.send*"]}`.
- **Change detection** (`--changed`) — per-node Diff / New / Old toggle, a **Changes** navigator
  (`N` / `Shift+N`), and deleted defs shown as removal diffs. Decorator edits count, and so do
  edits to a module-level name a function reads (`LIMIT = 500`): that function's diff opens with
  the line. New files you haven't `git add`ed, deleted files, and module-level code no function
  reads (`app.include_router(…)`) are shown too.
- **Review mode** — tick endpoints off as reviewed with a progress bar (persisted in localStorage).
  A tick belongs to the code you reviewed: when that route's code changes, it clears.
- **Keyboard first** — `/` filters endpoints, `J`/`K` moves between them, `F` fits, `?` lists the rest.
  Light and dark themes follow your system, with a toggle.

## How it works

`scripts/flowsegul_gen.py` AST-parses your Python, builds the call graph, and injects it as JSON
into `scripts/template.html` (a generic, self-contained viewer). No per-PR hand-authoring — it's
all derived from the source.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

A smoke test builds a tiny FastAPI repo, runs the generator in normal and `--changed` modes, and
checks the HTML it writes. CI runs it on Linux and macOS. `tests/test_react.py` checks React mode
on a small checkout app (`tests/react_fixture.py`), and `tests/test_react_review.py` the erased-state
mark on a two-commit orders app (`tests/react_review_fixture.py`); both skip when Node or
`typescript` isn't found.

## Claude Code skill

If you use [Claude Code](https://claude.com/claude-code), `./install.sh --skill` links flowsegul as
a skill. You can then ask Claude to "visualize the call flow for this PR" — and, more usefully, have
it **generate the graph and read it back to you for bugs** (variables produced but never consumed,
skipped layers, contract mismatches).

## Roadmap

- **FE consumers + contract linter** — cross the Python→TS boundary: for each endpoint, show the
  frontend call sites that consume it and flag BE-only / FE-only / type-mismatch fields.
