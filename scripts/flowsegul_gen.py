#!/usr/bin/env python3
"""Generate an interactive call-flow HTML from Python entry functions + source files.

AST-derives, per entry function, the tree of calls into functions/methods defined in the given
files: each node is a function, each step is an assignment/return that calls another in-scope
function, with the arguments passed, the callee's declared return type, and data-flow variables.
Renders the self-contained template.html (draggable graph, inline code, color-coded flow).

Usage:
  flowsegul_gen.py --repo DIR [--files A.py B.py ...] --out out.html [--template template.html]
                  [--entries name1 name2 | --routes] [--depth 5] [--title "..."]

Examples:
  # every @router.<method> endpoint in a controller, resolving calls across the service + repos:
  flowsegul_gen.py --repo ~/proj/be \
      --files src/rest/analytics_controller.py src/services/analytics_service.py \
              src/persistence/repositories \
      --routes --out flowsegul.html

  # explicit entry functions:
  flowsegul_gen.py --repo ~/proj/be --files src/services/foo.py --entries do_thing --out cf.html
"""
import argparse, ast, json, os, re, subprocess, sys, tempfile

PALETTE = ['#3b82f6','#0ea5a4','#d97706','#db2777','#16a34a','#7c3aed','#ea580c','#0891b2','#4f46e5','#059669']
INPUT_PALETTE = ['#e8590c','#1098ad','#9c36b5','#2f9e44','#c2255c','#1971c2']
SKIP_PARAMS = {'self', 'cls', 'db', 'body', 'request', 'session'}


_EXCLUDE = ('/.', 'venv', 'node_modules', '__pycache__', '/tests', '/test', '/alembic',
            '/migrations', '/.git')


def gather_py(paths):
    files = []
    for p in paths:
        if os.path.isdir(p):
            for root, _d, fs in os.walk(p):
                if any(x in root for x in _EXCLUDE):
                    continue
                files += [os.path.join(root, f) for f in fs
                          if f.endswith('.py') and not f.startswith('test_')]
        elif p.endswith('.py'):
            files.append(p)
    return sorted(set(files))


def git(repo, *args):
    try:
        return subprocess.check_output(['git', '-C', repo, *args],
                                       stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return ''


def source_dirs(repo):
    """Best-effort source roots to index for call resolution within a repo."""
    for cand in ('src', 'app', 'lib'):
        if os.path.isdir(os.path.join(repo, cand)):
            return [os.path.join(repo, cand)]
    return [repo]


def is_python_repo(d):
    return any(gather_py([sd]) for sd in source_dirs(d))


def discover_repos(workspace):
    """Python git repos to consider: the workspace's immediate git subdirs, or itself."""
    repos = []
    for name in sorted(os.listdir(workspace)):
        d = os.path.join(workspace, name)
        if os.path.isdir(os.path.join(d, '.git')) and is_python_repo(d):
            repos.append(d)
    if not repos and os.path.isdir(os.path.join(workspace, '.git')) and is_python_repo(workspace):
        repos = [workspace]
    return repos


def auto_workspace():
    """Umbrella dir holding the sibling repos. Works whether run from the umbrella or inside a repo."""
    cwd = os.getcwd()
    # run from the umbrella (contains git subdirs)?
    if any(os.path.isdir(os.path.join(cwd, n, '.git')) for n in os.listdir(cwd)
           if os.path.isdir(os.path.join(cwd, n))):
        return cwd
    top = git('.', 'rev-parse', '--show-toplevel')
    if top:
        parent = os.path.dirname(top)          # catch sibling repos next to this one
        if discover_repos(parent):
            return parent
        return top
    return cwd


def prefer_remote(repo, base):
    """Prefer origin/<base> over a local branch name, so a stale local `main` doesn't leak
    already-merged upstream commits into the diff. Explicit remotes/hashes pass through unchanged."""
    if '/' not in base and git(repo, 'rev-parse', '--verify', '--quiet', f'origin/{base}'):
        return f'origin/{base}'
    return base


def diff_base(repo, base, head, merge_base=True):
    """The commit to diff *from*: the merge-base of base..head (branch semantics), or `base`
    itself when merge_base is False (exact commit range, e.g. --from A --to B)."""
    return (git(repo, 'merge-base', base, head or 'HEAD') or base) if merge_base else base


def changed_rel_files(repo, base, head=None, merge_base=True):
    """Python files changed between `base` and `head`. Branch mode (merge_base=True) diffs the
    fork point, and for the current checkout (head=None) also counts staged/unstaged edits.
    Exact mode (merge_base=False) diffs `base`..`head` directly."""
    tip = head or 'HEAD'
    mb = diff_base(repo, base, head, merge_base)
    cmds = [['diff', '--name-only', mb, tip]]
    if head is None and merge_base:
        cmds += [['diff', '--name-only'], ['diff', '--name-only', '--cached']]
    names = set()
    for cmd in cmds:
        names |= {n for n in git(repo, *cmd).split('\n') if n.endswith('.py')}
    return {n for n in names if n}


def ref_py_files(repo, ref, dirs):
    """Python files that exist at `ref` under the given dirs (repo-relative)."""
    rels = [os.path.relpath(d, repo) for d in dirs]
    out = git(repo, 'ls-tree', '-r', '--name-only', ref, '--', *rels)
    return [n for n in out.split('\n')
            if n.endswith('.py') and not os.path.basename(n).startswith('test_')
            and not any(x in '/' + n for x in _EXCLUDE)]


def unparse(node):
    try:
        return ast.unparse(node)
    except Exception:
        return ''


def one_line(s, limit=140):
    s = re.sub(r'\s+', ' ', (s or '').strip())
    return s[:limit] + ('…' if len(s) > limit else '')


def model_fields(cls):
    """`name: type [= default]` class attributes of a ClassDef (Pydantic/dataclass field shape)."""
    out = []
    for m in cls.body:
        if isinstance(m, ast.AnnAssign) and isinstance(m.target, ast.Name):
            typ = unparse(m.annotation)
            optional = (m.value is not None) or bool(re.search(r'Optional\[|(\bNone\b)', typ))
            out.append({'name': m.target.id, 'type': typ, 'optional': optional})
    return out


def collect_models(names, idx, diffs=None, changed_m=frozenset()):
    """Registry {name: {name, sub, fields, nested, changed, diff?}} for `names` and every model
    reachable through their field types (so the drawer can drill into nested schemas)."""
    diffs = diffs or {}
    reg, queue = {}, list(names)
    while queue:
        nm = queue.pop()
        if nm in reg or nm not in idx.models:
            continue
        m = idx.models[nm]
        fields = m.get('fields', [])
        nested = sorted({t for f in fields
                         for t in re.findall(r'[A-Za-z_][A-Za-z0-9_]*', f['type'])
                         if t in idx.models and t != nm})
        entry = {'name': nm, 'sub': f"{m['file'].split('/')[-1]}:{m['lineno']}",
                 'fields': fields, 'nested': nested, 'changed': nm in changed_m}
        d = diffs.get('model:' + nm)
        if d:
            entry['diff'] = d['diff']; entry['base'] = d['base']; entry['isnew'] = d['new']
        reg[nm] = entry
        queue += nested
    return reg


class Index:
    """All functions/methods across the given files, with resolution helpers.

    If `ref` is set, source is read from that git ref (`git show ref:path`) instead of the working
    tree, so a branch can be inspected without checking it out.
    """
    def __init__(self, repo, files, ref=None):
        self.repo = repo
        self.ref = ref
        self.funcs = {}        # qual -> info
        self.by_method = {}    # method name -> [qual]
        self.by_simple = {}    # module func name -> [qual]
        self.routes = []       # (qual, method, path, prefix)
        self.steps_cache = {}  # qual -> steps (memoized)
        self.file_src = {}     # rel path -> exact source it was parsed from
        self.models = {}       # class name -> {code, file, lineno} (request/response schemas etc.)
        for f in files:
            self._index_file(f)

    def _rel(self, f):
        if self.ref and not os.path.isabs(f):
            return f
        try:
            return os.path.relpath(f, self.repo)
        except Exception:
            return f

    def _read(self, f):
        if self.ref:
            return git(self.repo, 'show', f'{self.ref}:{self._rel(f)}')
        try:
            return open(f).read()
        except Exception:
            return ''

    def _index_file(self, f):
        src = self._read(f)
        if not src:
            return
        try:
            tree = ast.parse(src)
        except SyntaxError:
            return
        rel = self._rel(f)
        self.file_src[rel] = src

        # router var -> prefix, e.g. `router = APIRouter(prefix="/analytics")`
        prefixes = {}
        for n in tree.body:
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call):
                fn = n.value.func
                if (getattr(fn, 'id', '') == 'APIRouter' or getattr(fn, 'attr', '') == 'APIRouter'):
                    pfx = next((unparse(k.value).strip('"\'') for k in n.value.keywords
                                if k.arg == 'prefix'), '')
                    for t in n.targets:
                        if isinstance(t, ast.Name):
                            prefixes[t.id] = pfx

        def add(node, cls):
            # qual is file-unique (rel::…) so same-named module functions in different files (e.g. a
            # controller route and a service fn both called get_usage_overview) don't collide.
            qual = f'{rel}::{cls}.{node.name}' if cls else f'{rel}::{node.name}'
            info = {
                'qual': qual, 'name': node.name, 'cls': cls, 'file': rel,
                'lineno': node.lineno, 'code': ast.get_source_segment(src, node) or '',
                'returns': unparse(node.returns) if node.returns else '',
                'params': [a.arg for a in node.args.args if a.arg not in SKIP_PARAMS],
                'node': node,
            }
            self.funcs[qual] = info
            (self.by_method if cls else self.by_simple).setdefault(node.name, []).append(qual)
            for dec in node.decorator_list:
                r = self._route(dec)
                if r:
                    method, path, var = r
                    prefix = prefixes.get(var, '')
                    self.routes.append((qual, method, prefix + path, prefix))

        for n in tree.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                add(n, None)
            elif isinstance(n, ast.ClassDef):
                self.models[n.name] = {'name': n.name, 'file': rel, 'lineno': n.lineno,
                                       'code': ast.get_source_segment(src, n) or '',
                                       'fields': model_fields(n)}
                for m in n.body:
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        add(m, n.name)

    @staticmethod
    def _route(dec):
        # @router.post("/path", ...) -> ("POST", "/path", "router")
        if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
            m = dec.func.attr.upper()
            if m in {'GET', 'POST', 'PUT', 'PATCH', 'DELETE'} and dec.args:
                a0 = dec.args[0]
                if isinstance(a0, ast.Constant):
                    var = dec.func.value.id if isinstance(dec.func.value, ast.Name) else ''
                    return m, a0.value, var
        return None

    def resolve(self, call, caller_cls=None, caller_qual=None):
        """Return the qual of an in-scope function this Call targets, else None.

        `self.x()` / `cls.x()` resolve only to a method `x` on the caller's own class. For
        `module.x()`, a candidate defined in a file named after the module (`usage_service.x` →
        usage_service.py) wins — this disambiguates name collisions such as a controller route and
        a service function both named `get_usage_overview`. Self-resolution is avoided when possible.
        """
        fn = call.func
        if isinstance(fn, ast.Attribute):
            recv, name = fn.value, fn.attr
            if isinstance(recv, ast.Name) and recv.id in ('self', 'cls'):
                caller_rel = caller_qual.split('::', 1)[0] if caller_qual else ''
                q = f'{caller_rel}::{caller_cls}.{name}'
                return q if caller_cls and q in self.funcs else None
            cands = self.by_method.get(name, []) + self.by_simple.get(name, [])
            if isinstance(recv, ast.Name):  # module alias hint, e.g. usage_service.get_x
                pref = [q for q in cands
                        if os.path.basename(self.funcs[q]['file'])[:-3] == recv.id]
                if pref:
                    cands = pref
        elif isinstance(fn, ast.Name):
            name = fn.id
            cands = self.by_simple.get(name, []) + self.by_method.get(name, [])
        else:
            return None
        if caller_qual and len(cands) > 1:  # don't resolve to yourself if there's an alternative
            cands = [c for c in cands if c != caller_qual] or cands
        return cands[0] if cands else None


def layer_of(rel):
    p = rel.lower()
    if 'controller' in p or '/rest/' in p or '/api/' in p or '/routes' in p:
        return 'controller'
    if '/services/' in p or 'service' in os.path.basename(p):
        return 'service'
    if 'repositor' in p or 'persistence' in p or '/dao' in p or '/db' in p:
        return 'repository'
    return 'helper'


def nid_for(qual):
    return re.sub(r'[^A-Za-z0-9]', '_', qual)


def models_in(annotation, idx):
    """Class names in an annotation string that are indexed models, e.g. 'list[UsersAnalytics]'."""
    return [t for t in re.findall(r'[A-Za-z_][A-Za-z0-9_]*', annotation or '') if t in idx.models]


def local_names(node):
    """Names of a function's variables: its parameters plus anything assigned (assignments,
    for-targets, with-as, comprehensions). Used to tint/hover them in the code."""
    names = set()
    a = node.args
    for arg in [*getattr(a, 'posonlyargs', []), *a.args, *a.kwonlyargs,
                a.vararg, a.kwarg]:
        if arg and arg.arg not in ('self', 'cls'):
            names.add(arg.arg)
    def add_targets(t):
        for nm in ast.walk(t):
            if isinstance(nm, ast.Name):
                names.add(nm.id)
    for n in ast.walk(node):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                add_targets(t)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            names.add(n.target.id)
        elif isinstance(n, (ast.For, ast.AsyncFor)):
            add_targets(n.target)
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            for it in n.items:
                if it.optional_vars:
                    add_targets(it.optional_vars)
        elif isinstance(n, ast.comprehension):
            add_targets(n.target)
    return sorted(names)


def funcs_in_source(src):
    """{qual: source_segment} for every function, method, and class ('model:Name') in `src`."""
    out = {}
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out

    def add(node, cls):
        out[f'{cls}.{node.name}' if cls else node.name] = ast.get_source_segment(src, node) or ''
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add(n, None)
        elif isinstance(n, ast.ClassDef):
            out['model:' + n.name] = ast.get_source_segment(src, n) or ''
            for m in n.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    add(m, n.name)
    return out


def def_linenos(src):
    """{key: lineno} for every def/class in `src` (parallel keys to funcs_in_source)."""
    out = {}
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[n.name] = n.lineno
        elif isinstance(n, ast.ClassDef):
            out['model:' + n.name] = n.lineno
            for m in n.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out[f'{n.name}.{m.name}'] = m.lineno
    return out


def changed_defs(idx, repo, base, ref, changed_files, merge_base=True):
    """(changed function quals, changed model names, {key: {base, diff, new}}, deleted) for defs
    whose source differs from `base`. `deleted` maps key → {name, file, lineno, base, diff, is_model}
    for defs that existed at base but were removed on the branch (no surviving node in HEAD)."""
    import difflib
    mb = diff_base(repo, base, ref, merge_base)
    quals, models, info, deleted = set(), set(), {}, {}
    for rel in changed_files:
        head_src = idx.file_src.get(rel)
        if head_src is None:
            continue  # changed file outside the indexed source
        base_src = git(repo, 'show', f'{mb}:{rel}')
        base_map = funcs_in_source(base_src)
        head_map = funcs_in_source(head_src)
        for key, hsrc in head_map.items():
            bsrc = base_map.get(key)
            if bsrc == hsrc:
                continue
            name = key[6:] if key.startswith('model:') else key
            # split WITHOUT keepends + lineterm='' so difflib never merges a no-trailing-newline last
            # line into the next. Full context for a function shows the whole function; a class uses
            # tight context so its Diff shows only the changed methods, not every unchanged one.
            ctx = 3 if key.startswith('model:') else 100000
            diff = '\n'.join(difflib.unified_diff(
                (bsrc or '').splitlines(), hsrc.splitlines(),
                fromfile=f'{name} (base)', tofile=f'{name} (branch)', lineterm='', n=ctx))
            if bsrc is not None and not diff.strip():
                continue  # only a trailing-whitespace/newline difference — not a real change
            (models.add(name) if key.startswith('model:')
             else quals.add(f'{rel}::{key}'))
            ikey = key if key.startswith('model:') else f'{rel}::{key}'
            info[ikey] = {'base': bsrc or '', 'diff': diff, 'new': bsrc is None}
        # deleted: existed at base, gone from HEAD (no surviving node — walk base, not head)
        base_lns = def_linenos(base_src)
        for key, bsrc in base_map.items():
            if key in head_map:
                continue
            name = key[6:] if key.startswith('model:') else key
            ikey = key if key.startswith('model:') else f'{rel}::{key}'
            diff = '\n'.join(difflib.unified_diff(
                bsrc.splitlines(), [], fromfile=f'{name} (base)', tofile=f'{name} (deleted)',
                lineterm='', n=100000))
            deleted[ikey] = {'name': name, 'file': rel, 'lineno': base_lns.get(key, 0),
                             'base': bsrc, 'diff': diff, 'is_model': key.startswith('model:')}
    return quals, models, info, deleted


def entry_models(idx, qual):
    node = idx.funcs[qual]['node']
    req = {m for a in node.args.args if a.annotation for m in models_in(unparse(a.annotation), idx)}
    resp = set(models_in(unparse(node.returns) if node.returns else '', idx))
    return req, resp


def reachable_quals(entry, idx, depth):
    """Every function reachable from `entry` within `depth` call levels."""
    seen, order, col, i = {entry}, [entry], {entry: 0}, 0
    while i < len(order):
        q = order[i]; i += 1
        if col[q] >= depth:
            continue
        for s in steps_for(idx.funcs[q], idx):
            cq = s['_qual']
            if cq not in seen:
                seen.add(cq); col[cq] = col[q] + 1; order.append(cq)
    return seen


def steps_for(info, idx):
    """Ordered data-flow steps: any statement that calls an in-scope function, recursing into
    if/for/while/with/try so calls in conditions and nested blocks are captured too. Memoized."""
    cache = idx.steps_cache
    if info['qual'] in cache:
        return cache[info['qual']]
    out, assigned = [], []
    src = idx.file_src.get(info['file'], '')
    caller_cls = info.get('cls')
    caller_qual = info['qual']

    def emit(var, expr_nodes, disp):
        calls = [c for en in expr_nodes for c in ast.walk(en)
                 if isinstance(c, ast.Call) and idx.resolve(c, caller_cls, caller_qual)]
        # indirect invocation: a function passed BY NAME as an argument, e.g.
        # `asyncio.to_thread(_build_feedback_workbook, records)` — a real call the resolver would miss
        refs = []
        for en in expr_nodes:
            for c in ast.walk(en):
                if isinstance(c, ast.Call):
                    for a in list(c.args) + [k.value for k in c.keywords]:
                        if isinstance(a, ast.Name):
                            cand = (idx.by_simple.get(a.id, []) + idx.by_method.get(a.id, []))
                            cand = [x for x in cand if x != caller_qual]
                            if cand:
                                refs.append(cand[0])
        direct_quals = {idx.resolve(c, caller_cls, caller_qual) for c in calls}
        refs = [q for q in dict.fromkeys(refs) if q not in direct_quals]
        if not calls and not refs:
            return
        try:
            seg = ast.get_source_segment(src, disp)
        except Exception:
            seg = None
        expr = one_line(seg or unparse(disp))
        uses = [v for v in assigned if re.search(r'\b' + re.escape(v) + r'\b', expr)]
        first = True
        for call in calls:
            qual = idx.resolve(call, caller_cls, caller_qual)
            out.append({
                'var': var, 'expr': expr if first else '', 'target': nid_for(qual),
                'arg': one_line(', '.join(unparse(a) for a in call.args)
                                + (', ' if call.args and call.keywords else '')
                                + ', '.join(f'{k.arg}={unparse(k.value)}' for k in call.keywords), 60),
                'ret': one_line(idx.funcs[qual]['returns'], 46), 'uses': uses, '_qual': qual,
            })
            first = False
        for qual in refs:
            out.append({
                'var': var, 'expr': expr if first else '', 'target': nid_for(qual), 'arg': '(deferred)',
                'ret': one_line(idx.funcs[qual]['returns'], 46), 'uses': uses, '_qual': qual,
            })
            first = False
        if var not in ('return', '·') and var not in assigned:
            assigned.append(var)

    def walk(stmts):
        for s in stmts:
            if isinstance(s, ast.Assign):
                emit(unparse(s.targets[0]), [s.value], s.value)  # RHS only — chip supplies "var ="
            elif isinstance(s, ast.AnnAssign) and s.value is not None:
                emit(unparse(s.target), [s.value], s.value)
            elif isinstance(s, ast.Return) and s.value is not None:
                emit('return', [s.value], s.value)  # value only — the row already prefixes "return"
            elif isinstance(s, (ast.If, ast.While)):
                emit('·', [s.test], s.test); walk(s.body); walk(s.orelse)
            elif isinstance(s, (ast.For, ast.AsyncFor)):
                emit('·', [s.iter], s.iter); walk(s.body); walk(s.orelse)
            elif isinstance(s, (ast.With, ast.AsyncWith)):
                emit('·', [it.context_expr for it in s.items], s.items[0].context_expr)
                walk(s.body)
            elif isinstance(s, ast.Try):
                walk(s.body)
                for h in s.handlers:
                    walk(h.body)
                walk(s.orelse); walk(s.finalbody)
            elif isinstance(s, ast.Expr):
                emit('·', [s.value], s)
            else:  # raise, assert, etc. — no nested statement bodies to recurse into
                emit('·', [s], s)

    walk(info['node'].body)
    cache[info['qual']] = out
    return out


def callers_of(idx, entry_qual, limit=8):
    """Functions that invoke `entry_qual`, for grafting caller context onto a rootless entry.
    Catches direct calls (via steps) AND bare references like `asyncio.to_thread(fn, …)`, which the
    Call-only resolver misses — the very reason such entries end up rootless."""
    name = idx.funcs[entry_qual]['name']
    is_method = bool(idx.funcs[entry_qual].get('cls'))
    out = []
    for q, info in idx.funcs.items():
        if q == entry_qual:
            continue
        direct = next((s for s in steps_for(info, idx) if s['_qual'] == entry_qual), None)
        ref = False
        if not direct and not is_method:   # bare Name passed around (to_thread, callbacks, …)
            ref = any(isinstance(nd, ast.Name) and nd.id == name for nd in ast.walk(info['node']))
        if not (direct or ref):
            continue
        out.append({'qual': q, 'info': info, 'step': direct})
        if len(out) >= limit:
            break
    return out


def build_endpoint(entry_qual, idx, meta, depth, tag, changed_q=frozenset(), changed_m=frozenset(),
                   diffs=None):
    """BFS the call tree from entry_qual into an endpoint spec, ids/fnKeys namespaced by `tag`.

    Nodes whose qual is in `changed_q` (or model name in `changed_m`) are flagged changed=True and
    carry their unified diff from `diffs`.
    """
    diffs = diffs or {}
    nid = lambda q: f'{tag}__{nid_for(q)}'
    fk = lambda q: f'{tag}::{q}'
    col = {entry_qual: 0}
    order = [entry_qual]
    seen = {entry_qual}
    i = 0
    while i < len(order):
        q = order[i]; i += 1
        if col[q] >= depth:
            continue
        for s in steps_for(idx.funcs[q], idx):
            cq = s['_qual']
            if cq not in col:
                col[cq] = col[q] + 1
            if cq not in seen:
                seen.add(cq); order.append(cq)
    nodes = []
    for q in order:
        info = idx.funcs[q]
        steps = []
        for s in steps_for(info, idx):
            s = {k: v for k, v in s.items() if k != '_qual'}
            s['target'] = f"{tag}__{s['target']}"
            steps.append(s)
        n = {
            'id': nid(q), 'fnKey': fk(q), 'col': col[q], 'entry': q == entry_qual,
            'layer': layer_of(info['file']), 'changed': q in changed_q,
            'title': (f"{info['cls']}." if info['cls'] else '') + info['name'],
            'sub': f"{info['file'].split('/')[-1]}:{info['lineno']}", 'steps': steps,
            'locals': local_names(info['node']),
        }
        if q in diffs:
            n['base'] = diffs[q]['base']; n['diff'] = diffs[q]['diff']; n['isnew'] = diffs[q]['new']
        nodes.append(n)
    # rootless entry (no route reaches it): graft its callers as collapsed "ghost" nodes to the left,
    # so the flow is reconnected without pulling in each caller's whole tree. They're clickable/expandable.
    if not meta.get('method'):
        present = {n['id'] for n in nodes}
        for c in callers_of(idx, entry_qual):
            cq, cinfo, src = c['qual'], c['info'], c['step']
            gid = nid(cq)
            if gid in present:
                continue
            present.add(gid)
            step = {'var': src['var'] if src else '', 'expr': (src['expr'] if src else '') or '',
                    'target': nid(entry_qual), 'arg': src['arg'] if src else '',
                    'ret': src['ret'] if src else '', 'uses': []}
            nodes.append({
                'id': gid, 'fnKey': fk(cq), 'col': -1, 'entry': False, 'ghost': True,
                'layer': layer_of(cinfo['file']), 'changed': False,
                'title': (f"{cinfo['cls']}." if cinfo['cls'] else '') + cinfo['name'],
                'sub': f"{cinfo['file'].split('/')[-1]}:{cinfo['lineno']}", 'steps': [step],
            })
    # request/response schemas from the entry's annotations. These are data shapes, not calls, so
    # they are NOT drawn as graph nodes — they surface as clickable chips that open a field drawer.
    entry_node = idx.funcs[entry_qual]['node']
    req_names = list(dict.fromkeys(m for a in entry_node.args.args if a.annotation
                                   for m in models_in(unparse(a.annotation), idx)))
    resp_names = list(dict.fromkeys(models_in(unparse(entry_node.returns) if entry_node.returns else '', idx)))
    models_reg = collect_models(req_names + resp_names, idx, diffs, changed_m)

    cand = [entry_qual] + [q for q in order if col[q] == 1]
    uniq = list(dict.fromkeys(p for q in cand for p in idx.funcs[q]['params']))[:len(INPUT_PALETTE)]
    inputs = [[p, INPUT_PALETTE[k], []] for k, p in enumerate(uniq)]
    doc = ast.get_docstring(idx.funcs[entry_qual]['node']) or ''
    prefix = meta.get('prefix', '')
    return {
        'id': nid(entry_qual), 'group': meta.get('groupOverride') or (tag + prefix if prefix else tag),
        'method': meta.get('method', 'fn'),
        'path': meta.get('path', idx.funcs[entry_qual]['name']),
        'title': idx.funcs[entry_qual]['name'],
        'summary': one_line(doc.split('\n')[0]) if doc else '',
        'inputs': inputs, 'nodes': nodes, 'modelEdges': [],
        'reqModels': req_names, 'respModels': resp_names, 'models': models_reg,
    }


def process_repo(repo, args):
    """Index a repo and return (endpoints, code) for the chosen entries; ids namespaced by repo name."""
    tag = os.path.basename(repo.rstrip('/'))
    dirs = [os.path.join(repo, p) for p in args.files] if args.files else source_dirs(repo)

    # Two modes: exact commit range (--from/--to) or branch semantics (--base/--branch, merge-base).
    exact = bool(args.commit_from)
    if exact:
        base, ref, changed = args.commit_from, args.commit_to, True
    else:
        base, ref, changed = prefer_remote(repo, args.base), args.branch, args.changed

    files = ref_py_files(repo, ref, dirs) if ref else gather_py(dirs)
    idx = Index(repo, files, ref=ref)
    if not idx.funcs:
        return [], {}

    # function-level change detection (which defs actually differ from base)
    changed_q, changed_m, diffs, deleted = set(), set(), {}, {}
    if changed:
        cf = changed_rel_files(repo, base, ref, merge_base=not exact)
        changed_q, changed_m, diffs, deleted = changed_defs(idx, repo, base, ref, cf, merge_base=not exact)

    def flow_touches_changed(qual):
        """Affected if the endpoint's flow reaches a changed FUNCTION, or its request/response
        SCHEMA (including nested schemas) changed. It does NOT flag an endpoint merely because some
        function deep in its flow takes/returns an ORM type (e.g. Meeting) that gained an unrelated
        field — that doesn't change this endpoint's behaviour."""
        reach = reachable_quals(qual, idx, 10 ** 6)  # unbounded: affectedness ≠ render depth
        if reach & changed_q:
            return reach, True
        req, resp = entry_models(idx, qual)
        schema_tree = set(collect_models(list(req | resp), idx).keys())
        if schema_tree & changed_m:
            return reach, True
        return reach, False

    build = lambda q, meta: build_endpoint(q, idx, meta, args.depth, tag, changed_q, changed_m, diffs)
    rendered_quals = lambda e: {n['fnKey'].split('::', 1)[1] for n in e['nodes']
                                if not n['fnKey'].split('::', 1)[1].startswith('model:')}
    del_code = {}  # code entries for synthetic DELETED nodes (no HEAD source to look up)

    if args.entries:
        endpoints = [build(q, {}) for name in args.entries
                     for q in idx.by_simple.get(name, []) + idx.by_method.get(name, [])]
    elif not changed:
        endpoints = [build(q, {'method': m, 'path': p, 'prefix': pre})
                     for q, m, p, pre in idx.routes]
    else:  # --changed: routes whose flow was changed somehow, then a coverage pass for the rest
        endpoints = [build(q, {'method': m, 'path': p, 'prefix': pre})
                     for q, m, p, pre in idx.routes if flow_touches_changed(q)[1]]
        # coverage guarantee: every changed function must appear as a node in ≥1 chart. Any changed
        # def not actually rendered (e.g. deeper than --depth, or reached only by non-route code)
        # becomes its own root chart, so nothing in the diff is invisible.
        shown = set().union(*(rendered_quals(e) for e in endpoints)) if endpoints else set()
        for q in sorted(changed_q - shown):
            if q not in idx.funcs or q in shown:
                continue
            e = build(q, {'groupOverride': f'{tag} ⟂ changed defs (no route)'})
            endpoints.append(e)
            shown |= rendered_quals(e)
        # class coverage: a changed class (schema / ORM model) that isn't shown as a chip and whose
        # change isn't already a shown method (e.g. an added column, or a *removed* method) gets its
        # own single-node diff chart, so nothing class-level in the diff is invisible either.
        chip_models = set().union(*((e.get('models') or {}).keys() for e in endpoints)) if endpoints else set()
        for name in sorted(changed_m):
            if name in chip_models or name not in idx.models:
                continue
            # skip if a changed METHOD of this class is already drawn (quals are rel::Class.method)
            if any(q in shown and q.split('::', 1)[-1].startswith(name + '.') for q in changed_q):
                continue
            m = idx.models[name]
            node = {'id': f'{tag}__model_{nid_for(name)}', 'fnKey': f'{tag}::model:{name}',
                    'col': 0, 'entry': True, 'layer': 'model', 'changed': True, 'title': name,
                    'sub': f"{m['file'].split('/')[-1]}:{m['lineno']}", 'steps': []}
            d = diffs.get('model:' + name)
            if d:
                node['base'] = d['base']; node['diff'] = d['diff']; node['isnew'] = d['new']
            endpoints.append({
                'id': node['id'], 'group': f'{tag} ⟂ changed defs (no route)', 'method': 'class',
                'path': name, 'title': name, 'summary': 'changed class / data model',
                'inputs': [], 'nodes': [node], 'modelEdges': [], 'reqModels': [], 'respModels': [],
                'models': collect_models([name], idx, diffs, changed_m)})
        # deleted defs: existed at base, removed on the branch — no HEAD node carries them, so render
        # each as its own DELETED node showing the removal diff.
        for ikey, dd in deleted.items():
            # a removed METHOD of a class that still exists is already shown by that class's diff node
            if not dd['is_model'] and '.' in dd['name'] and dd['name'].split('.')[0] in idx.models:
                continue
            nidd, fkey = f'{tag}__del_{nid_for(ikey)}', f'{tag}::del:{ikey}'
            node = {'id': nidd, 'fnKey': fkey, 'col': 0, 'entry': True, 'changed': True,
                    'deleted': True, 'layer': 'model' if dd['is_model'] else 'helper',
                    'title': dd['name'], 'sub': f"{dd['file'].split('/')[-1]}:{dd['lineno']}",
                    'steps': [], 'base': dd['base'], 'diff': dd['diff'], 'isnew': False}
            del_code[fkey] = {'file': dd['file'], 'lineno': dd['lineno'], 'name': dd['name'],
                              'cls': None, 'code': ''}
            endpoints.append({
                'id': nidd, 'group': f'{tag} ⟂ changed defs (no route)', 'method': 'del',
                'path': dd['name'], 'title': dd['name'], 'summary': 'deleted definition',
                'inputs': [], 'nodes': [node], 'modelEdges': [], 'reqModels': [], 'respModels': [],
                'models': {}})
    code = dict(del_code)
    for e in endpoints:
        for n in e['nodes']:
            q = n['fnKey'].split('::', 1)[1]
            if q.startswith('del:'):
                continue  # DELETED node — code already provided by del_code
            if q.startswith('model:'):
                m = idx.models[q.split('model:', 1)[1]]
                code[n['fnKey']] = {'file': m['file'], 'lineno': m['lineno'],
                                    'name': m['name'], 'cls': None, 'code': m['code']}
            else:
                info = idx.funcs[q]
                code[n['fnKey']] = {'file': info['file'], 'lineno': info['lineno'],
                                    'name': info['name'], 'cls': info['cls'], 'code': info['code']}
    return endpoints, code


def main():
    ap = argparse.ArgumentParser(description='Auto-generate an interactive call-flow explorer.')
    ap.add_argument('--workspace', help='umbrella dir holding the sibling repos (auto-detected)')
    ap.add_argument('--repo', action='append', dest='repos', help='specific repo(s); repeatable')
    ap.add_argument('--files', nargs='+', help='files/dirs to index within each repo (default: auto src/)')
    ap.add_argument('--entries', nargs='*', help='entry function names (default: FastAPI @router endpoints)')
    ap.add_argument('--changed', action='store_true', help='only entries in files changed vs --base')
    ap.add_argument('--base', default='main', help='base ref to diff against (default main)')
    ap.add_argument('--branch', help='inspect this git ref instead of the working tree (no checkout '
                    'needed); combine with --changed to see what it changed vs --base')
    ap.add_argument('--from', dest='commit_from', metavar='REF',
                    help='diff exactly between two commits/refs by hash: the "before" commit. '
                    'Diffs FROM..TO directly (no merge-base), and implies --changed.')
    ap.add_argument('--to', dest='commit_to', metavar='REF',
                    help='the "after" commit for --from (default: the working tree / HEAD)')
    ap.add_argument('--depth', type=int, default=6)
    ap.add_argument('--title', default='Call-Flow Explorer')
    ap.add_argument('--template', default=os.path.join(os.path.dirname(__file__), 'template.html'))
    ap.add_argument('--out', default=os.path.join(tempfile.gettempdir(), 'flowsegul.html'))
    args = ap.parse_args()

    repos = args.repos or discover_repos(args.workspace or auto_workspace())
    repos = [os.path.abspath(r) for r in repos]
    if not repos:
        sys.exit('No Python repos found. Pass --repo DIR or run from your repos umbrella.')

    all_eps, all_code = [], {}
    for repo in repos:
        print(f'  scanning {os.path.basename(repo)}…', file=sys.stderr, flush=True)
        eps, code = process_repo(repo, args)
        all_eps += eps; all_code.update(code)
    if not all_eps:
        hint = ' changed on this branch' if (args.changed or args.commit_from) else ''
        sys.exit(f'No entries found{hint} across: {", ".join(os.path.basename(r) for r in repos)}.')
    all_eps.sort(key=lambda e: e.get('group', ''))  # keep same-group endpoints contiguous

    blob = json.dumps({'code': all_code, 'graph': {'endpoints': all_eps}}, ensure_ascii=False)
    tpl = open(args.template).read()
    if '__DATA__' not in tpl:
        sys.exit('template missing __DATA__ placeholder')
    tpl = tpl.replace('<title>KPI Endpoints — Call-Flow Explorer</title>', f'<title>{args.title}</title>', 1)
    open(args.out, 'w').write(tpl.replace('__DATA__', blob))
    groups = {}
    for e in all_eps:
        groups[e['group']] = groups.get(e['group'], 0) + 1
    print(f'Wrote {args.out}')
    print('  ' + ' · '.join(f'{g}: {n}' for g, n in groups.items())
          + f'  ({len(all_code)} functions)')


if __name__ == '__main__':
    main()
