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
import argparse, ast, copy, fnmatch, html, json, os, re, subprocess, sys, tempfile

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


def http_status(node):
    """Status code of an `HTTPException(404, …)` / `HTTPException(status_code=status.HTTP_404_…)`
    call, else None."""
    if not isinstance(node, ast.Call):
        return None
    cand = [k.value for k in node.keywords if k.arg == 'status_code'] + list(node.args[:1])
    for v in cand:
        if isinstance(v, ast.Constant) and isinstance(v.value, int):
            return v.value
        m = re.search(r'(\d{3})', unparse(v))
        if m:
            return int(m.group(1))
    return None


def status_in(fn):
    """The status an exception handler answers with, e.g. `JSONResponse(status_code=400, …)`."""
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            st = http_status(n) if any(k.arg == 'status_code' for k in n.keywords) else None
            if st:
                return st
    return None


def exc_name(node):
    """`raise ValueError(...)` / `raise errors.NotFound` -> 'ValueError' / 'NotFound'."""
    if isinstance(node, ast.Call):
        node = node.func
    return unparse(node).split('.')[-1] if node is not None else ''


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
        self.facts_cache = {}  # qual -> value_facts (memoized)
        self.file_src = {}     # rel path -> exact source it was parsed from
        self.models = {}       # class name -> {code, file, lineno} (request/response schemas etc.)
        self.bases = {}        # class name -> [base class names], to match `except Base` to a subclass
        self.app_handlers = {} # exception name -> status (or None) from @app.exception_handler(...)
        self.raise_cache = {}  # qual -> own raise statements (filled by steps_for)
        self.esc_cache = {}    # qual -> errors that can leave the function (see escapes)
        self.layers = load_config(repo).get('layers') or {}
        for f in files:
            self._index_file(f)
        self.route_quals = {r[0] for r in self.routes}

    def layer(self, qual):
        """A route handler is a controller wherever it lives; otherwise go by its file."""
        if qual in self.route_quals:
            return 'controller'
        return layer_of(self.funcs[qual]['file'], self.layers)

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

        self._exception_handlers(tree)
        for n in tree.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                add(n, None)
            elif isinstance(n, ast.ClassDef):
                self.bases[n.name] = [unparse(b).split('.')[-1] for b in n.bases]
                self.models[n.name] = {'name': n.name, 'file': rel, 'lineno': n.lineno,
                                       'code': ast.get_source_segment(src, n) or '',
                                       'fields': model_fields(n)}
                for m in n.body:
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        add(m, n.name)

    def _exception_handlers(self, tree):
        """`@app.exception_handler(ValueError)` and `app.add_exception_handler(ValueError, fn)`: the
        app turns those errors into a response, so they don't end up as a 500."""
        fns = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for fn in fns.values():
            for dec in fn.decorator_list:
                if (isinstance(dec, ast.Call) and getattr(dec.func, 'attr', '') == 'exception_handler'
                        and dec.args):
                    self.app_handlers[unparse(dec.args[0]).split('.')[-1]] = status_in(fn)
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call) and getattr(n.func, 'attr', '') == 'add_exception_handler'
                    and len(n.args) >= 2):
                h = fns.get(unparse(n.args[1]))
                self.app_handlers[unparse(n.args[0]).split('.')[-1]] = status_in(h) if h else None

    def is_sub(self, t, base, seen=None):
        """Is exception class `t` the class `base` or derived from it (as far as the repo shows)?"""
        if t == base:
            return True
        seen = seen or set()
        for b in self.bases.get(t, []):
            if b not in seen:
                seen.add(b)
                if self.is_sub(b, base, seen):
                    return True
        return False

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


# Kinds of code beyond controller → service → repository, recognised by file or folder name.
# Checked against the file's own name first, then its folders from the innermost outwards, so
# `services/utils.py` is a util and `utils/service_client.py` is a service. First hit wins.
_KIND_EXACT = {
    'controller': {'api', 'rest', 'routes', 'routers', 'router', 'endpoints', 'views', 'handlers'},
    'repository': {'persistence', 'dao', 'daos', 'db', 'crud', 'queries', 'store', 'stores', 'db_access'},
    'model': {'models', 'model', 'entities', 'entity', 'orm', 'tables', 'domain_models'},
    'schema': {'schemas', 'schema', 'dto', 'dtos', 'serializers', 'contracts'},
    'factory': {'factory', 'factories', 'builders', 'builder', 'fixtures', 'seeds'},
    'util': {'utils', 'util', 'helpers', 'helper', 'common', 'shared', 'tools', 'misc', 'lib'},
    'task': {'tasks', 'task', 'workers', 'worker', 'jobs', 'job', 'celery', 'consumers', 'cron', 'scheduler'},
    'external': {'clients', 'client', 'integrations', 'adapters', 'gateways', 'gateway', 'external', 'sdk'},
    'config': {'config', 'configs', 'settings', 'conf', 'env'},
    'migration': {'alembic', 'migrations', 'migration'},
}
_KIND_SUB = [('controller', 'controller'), ('service', 'service'), ('repositor', 'repository'),
             ('factor', 'factory'), ('util', 'util'), ('client', 'external')]
_GENERIC_DIRS = {'src', 'app', 'apps', 'lib', 'pkg', 'core', 'main', 'python', 'code', ''}


def _kind_of_token(tok):
    for kind, names in _KIND_EXACT.items():
        if tok in names:
            return kind
    for sub, kind in _KIND_SUB:
        if sub in tok:
            return kind
    return None


def load_config(repo):
    """Optional `.flowsegul.json` at the repo root: {"layers": {"<glob>": "<label>"}} maps paths
    (fnmatch on the repo-relative path, e.g. "src/app/core/**") to a layer label of your choosing."""
    try:
        with open(os.path.join(repo, '.flowsegul.json')) as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def layer_of(rel, overrides=None):
    """Layer label for a repo-relative file: a config override, a known kind (controller, service,
    repository, model, schema, factory, util, task, external, config, migration), or — for code
    that fits none of them — the name of its folder, so an unfamiliar layout still reads sensibly."""
    for pat, label in (overrides or {}).items():
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, pat.rstrip('/*') + '/*'):
            return re.sub(r'[^a-z0-9_-]', '-', str(label).lower())[:20] or 'other'
    parts = rel.lower().replace('\\', '/').split('/')
    base = parts[-1][:-3] if parts[-1].endswith('.py') else parts[-1]
    for tok in [base] + parts[-2::-1]:
        k = _kind_of_token(tok)
        if k:
            return k
    folder = next((p for p in parts[-2::-1] if p not in _GENERIC_DIRS), '')
    return re.sub(r'[^a-z0-9_-]', '-', folder)[:20] or 'module'


# sidebar badge for entries that are not HTTP routes
_KIND_BADGE = {'util': 'util', 'factory': 'fact', 'model': 'class', 'schema': 'class',
               'migration': 'mig', 'task': 'task', 'external': 'ext', 'config': 'cfg'}


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


def _chain(node):
    """('payload', '.items') for a Name/Attribute chain like `payload.items`, else None."""
    attrs = []
    while isinstance(node, ast.Attribute):
        attrs.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return node.id, ''.join('.' + a for a in reversed(attrs))
    return None


def param_names(node):
    a = node.args
    return [x.arg for x in [*getattr(a, 'posonlyargs', []), *a.args, *a.kwonlyargs]
            if x.arg not in ('self', 'cls')]


def value_facts(info, idx):
    """What one function does with the values it holds, for tracing data across calls. Memoized.

    aliases: {local: (name, path)} for locals that are just another name for part of a value:
             `items = payload.items`, `for i in items` (i is an element of items: path '[]').
    reads:   {param: sorted paths} fields a parameter is read at, e.g. payload → ['.coupon', '.items'],
             following aliases (so `i.quantity` in `for i in items` is items → '[].quantity').
    whole:   {param: [callee]} a parameter handed whole to code flowsegul can't see into, so any of
             its fields may be used there.
    """
    cache = idx.facts_cache
    if info['qual'] in cache:
        return cache[info['qual']]
    fn = info['node']
    params = set(param_names(fn))
    aliases = {}
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            c = _chain(n.value)
            if c and c[0] not in ('self', 'cls') and c[0] != n.targets[0].id:
                aliases[n.targets[0].id] = c
        elif isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension)) and isinstance(n.target, ast.Name):
            c = _chain(n.iter)
            if c and c[0] != n.target.id:
                aliases[n.target.id] = (c[0], c[1] + '[]')

    def resolve(name, path=''):
        for _ in range(12):
            if name in params or name not in aliases:
                break
            name, pre = aliases[name]
            path = pre + path
        return name, path

    parent = {}
    for n in ast.walk(fn):
        for ch in ast.iter_child_nodes(n):
            parent[ch] = n
    reads, whole = {}, {}
    for n in ast.walk(fn):
        if isinstance(n, ast.Attribute) and not isinstance(parent.get(n), ast.Attribute):
            c = _chain(n)
            if not c:
                continue
            name, path = c
            up = parent.get(n)
            if isinstance(up, ast.Call) and up.func is n:   # payload.items.append(…) reads .items
                path = path[:path.rfind('.')]
            root, pre = resolve(name)
            if root in params and pre + path:
                reads.setdefault(root, set()).add(pre + path)
        elif isinstance(n, ast.Call) and not idx.resolve(n, info.get('cls'), info['qual']):
            callee = unparse(n.func).split('(')[0][-40:]
            for a in list(n.args) + [k.value for k in n.keywords]:
                if isinstance(a, ast.Starred):
                    a = a.value
                if isinstance(a, ast.Name):
                    root, pre = resolve(a.id)
                    if root in params and not pre:
                        whole.setdefault(root, [])
                        if callee not in whole[root]:
                            whole[root].append(callee)
    out = {'aliases': aliases, 'resolve': resolve, 'params': params,
           'reads': {k: sorted(v) for k, v in reads.items()}, 'whole': whole}
    cache[info['qual']] = out
    return out


def bindings_for(call, callee):
    """[{p: callee param, from: caller name, path, text}] — which argument lands in which parameter.
    An argument that is a computed expression gets `uses` (the names it's computed from) instead."""
    params = param_names(callee['node'])
    pairs = []
    for i, a in enumerate(call.args):
        if isinstance(a, ast.Starred) or i >= len(params):
            break
        pairs.append((params[i], a))
    for k in call.keywords:
        if k.arg and k.arg in params:
            pairs.append((k.arg, k.value))
    out = []
    for p, expr in pairs:
        b = {'p': p, 'text': one_line(unparse(expr), 40)}
        c = _chain(expr)
        if c and c[0] not in ('self', 'cls'):
            b['from'], b['path'] = c
        else:
            b['uses'] = sorted({n.id for n in ast.walk(expr) if isinstance(n, ast.Name)
                                and n.id not in ('self', 'cls')})[:4]
        out.append(b)
    return out


def steps_for(info, idx):
    """Ordered data-flow steps: any statement that calls an in-scope function, recursing into
    if/for/while/with/try so calls in conditions and nested blocks are captured too. Memoized."""
    cache = idx.steps_cache
    if info['qual'] in cache:
        return cache[info['qual']]
    out, assigned, raises = [], [], []
    catching = []   # stack of enclosing try blocks: [[(exception names, status or None, re-raises)]]
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
                'bind': bindings_for(call, idx.funcs[qual]),
                '_catch': [h for blk in catching for h in blk],
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
                catching.append([handler_info(h) for h in s.handlers])
                walk(s.body)
                catching.pop()
                for h in s.handlers:
                    walk(h.body)
                walk(s.orelse); walk(s.finalbody)
            elif isinstance(s, ast.Expr):
                emit('·', [s.value], s)
            else:  # raise, assert, etc. — no nested statement bodies to recurse into
                if isinstance(s, ast.Raise) and s.exc is not None:
                    try:
                        seg = ast.get_source_segment(src, s)
                    except Exception:
                        seg = None
                    raises.append({'t': exc_name(s.exc), 'status': http_status(s.exc),
                                   'text': one_line(seg or unparse(s), 90),
                                   '_catch': [h for blk in catching for h in blk]})
                emit('·', [s], s)

    walk(info['node'].body)
    cache[info['qual']] = out
    idx.raise_cache[info['qual']] = raises
    return out


def handler_info(h):
    """One `except` clause: (names it catches, status it answers with, whether it raises on)."""
    if h.type is None:
        names = ['*']
    elif isinstance(h.type, ast.Tuple):
        names = [unparse(e).split('.')[-1] for e in h.type.elts]
    else:
        names = [unparse(h.type).split('.')[-1]]
    status, reraises = None, False
    for n in ast.walk(ast.Module(body=h.body, type_ignores=[])):
        if isinstance(n, ast.Raise):
            st = http_status(n.exc) if n.exc is not None else None
            if st:
                status = status or st
            else:
                reraises = True   # `raise` / `raise OtherError`: the error keeps going
    return (names, status, reraises)


def caught_by(t, handlers, idx):
    """The handler that stops error `t`, or None if it keeps going up."""
    for names, status, reraises in handlers:
        if any(n in ('*', 'Exception', 'BaseException') or idx.is_sub(t, n) for n in names):
            return None if reraises and not status else (names, status, reraises)
    return None


def escapes(qual, idx, _stack=None):
    """Errors that can leave `qual`: its own uncaught `raise`s, plus whatever its callees let out
    that no `except` around the call stops. Each is (type, status or None, origin qual, raise text)."""
    if qual in idx.esc_cache:
        return idx.esc_cache[qual]
    stack = _stack or set()
    if qual in stack:          # recursion: don't loop
        return []
    stack.add(qual)
    info = idx.funcs[qual]
    steps = steps_for(info, idx)
    out = []
    for r in idx.raise_cache.get(qual, []):
        if not caught_by(r['t'], r['_catch'], idx):
            out.append((r['t'], r['status'], qual, r['text']))
    for st in steps:
        cq = st.get('_qual')
        if not cq or cq not in idx.funcs:
            continue
        for e in escapes(cq, idx, stack):
            if not caught_by(e[0], st.get('_catch', []), idx):
                out.append(e)
    stack.discard(qual)
    out = list(dict.fromkeys(out))
    if not _stack or len(stack) == 0:
        idx.esc_cache[qual] = out
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


def trace_values(order, entry_qual, idx, nid):
    """Follow values across the calls of one endpoint.

    Every value gets an origin: the (node id|name) where it first appears, plus the path into it.
    The entry's parameters and every other function's own locals are origins; a parameter takes
    the origin of whatever the caller passed, so `apply_coupon(subtotal, payload.coupon)` makes
    apply_coupon's `total` the caller's `subtotal`, and its `coupon` the entry's `payload` at
    `.coupon`. Returns ({qual: {name: [[origin, path], …]}}, {qual: [[param, text, caller title]]}).
    """
    facts = {q: value_facts(idx.funcs[q], idx) for q in order}
    origins = {}

    def origin_of(q, name, path=''):
        root, pre = facts[q]['resolve'](name)
        base = origins.get((q, root)) or {(f'{nid(q)}|{root}', '')}
        return {(o, p + pre + path) for o, p in base}

    recv = {}
    for _ in range(3):  # callers come before callees in BFS order; a few passes settle cycles
        changed = False
        for q in order:
            info = idx.funcs[q]
            title = (f"{info['cls']}." if info['cls'] else '') + info['name']
            for st in steps_for(info, idx):
                cq = st['_qual']
                if cq not in facts:
                    continue
                for b in st.get('bind') or []:
                    r = recv.setdefault(cq, [])
                    if [b['p'], b['text'], title] not in r and len(r) < 8:
                        r.append([b['p'], b['text'], title])
                    if 'from' not in b:
                        continue  # computed argument: a new value, its origin is the parameter itself
                    got = origins.setdefault((cq, b['p']), set())
                    new = origin_of(q, b['from'], b['path']) - got
                    if new and len(got) < 8:
                        got |= new
                        changed = True
        if not changed:
            break
    flow = {}
    for q in order:
        f = facts[q]
        names = set(f['params']) | set(f['aliases'])
        out = {}
        for nm in sorted(names):
            o = origin_of(q, nm)
            if o != {(f'{nid(q)}|{nm}', '')}:
                out[nm] = sorted([list(x) for x in o])[:8]
        flow[q] = out
    return flow, recv, facts


_PATH_TOK = re.compile(r'\.([A-Za-z_][A-Za-z0-9_]*)|\[\]')


def field_use(entry_qual, order, flow, facts, idx, nid):
    """{model: {field: [function titles]}} for the entry's request models: which function reads
    which field, following the value through every call. A model handed whole to code flowsegul
    can't see into is recorded under '*'."""
    enode = idx.funcs[entry_qual]['node']
    roots = {}
    for a in enode.args.args + enode.args.kwonlyargs:
        ms = models_in(unparse(a.annotation), idx) if a.annotation else []
        if ms:
            roots[f'{nid(entry_qual)}|{a.arg}'] = ms[0]
    use = {}

    def mark(model, path, who):
        cur = model
        toks = list(_PATH_TOK.finditer(path))
        if not toks:
            use.setdefault(cur, {}).setdefault('*', [])
            if who not in use[cur]['*']:
                use[cur]['*'].append(who)
            return
        for t in toks:
            if not t.group(1):
                continue  # [] — an element of a list keeps the list's model
            fld = t.group(1)
            use.setdefault(cur, {}).setdefault(fld, [])
            if who not in use[cur][fld]:
                use[cur][fld].append(who)
            f = next((x for x in idx.models.get(cur, {}).get('fields', []) if x['name'] == fld), None)
            nxt = models_in(f['type'], idx) if f else []
            if not nxt:
                return
            cur = nxt[0]

    for q in order:
        info = idx.funcs[q]
        who = (f"{info['cls']}." if info['cls'] else '') + info['name']
        own = f'{nid(q)}|'
        for nm, paths in facts[q]['reads'].items():
            for o, p in (flow[q].get(nm) or [[own + nm, '']]):
                if o in roots:
                    for rp in paths:
                        mark(roots[o], p + rp, who)
        for nm, callees in facts[q]['whole'].items():
            for o, p in (flow[q].get(nm) or [[own + nm, '']]):
                if o in roots:
                    # a field handed on (`round(coupon)`) is just read; a whole model handed on may
                    # have any field read, so say where it went
                    mark(roots[o], p, who if p else f"{who} → {', '.join(callees[:2])}()")
    return use


def app_status(t, idx):
    """Status an app-level exception handler gives error `t`: an int, 0 if handled with an unknown
    status, None if no handler covers it."""
    for name, st in idx.app_handlers.items():
        if idx.is_sub(t, name):
            return st or 0
    return None


def statuses_for(entry_qual, idx, changed_q):
    """What the route can answer with: its success status, every HTTPException status reachable,
    statuses app-level handlers give, and 500 for errors nothing catches. `new` marks answers
    that come from a changed function."""
    node = idx.funcs[entry_qual]['node']
    ok = 200
    for dec in node.decorator_list:
        if isinstance(dec, ast.Call):
            st = next((http_status(dec) for k in dec.keywords if k.arg == 'status_code'), None)
            ok = st or ok
    out = {str(ok): {'code': str(ok), 'kind': 'ok', 'types': [], 'new': False}}
    for t, st, origin, text in escapes(entry_qual, idx):
        if st:
            code, kind = str(st), ('bad' if st >= 500 else 'http')
        else:
            a = app_status(t, idx)
            code, kind = ((str(a) if a else 'handled'), 'http') if a is not None else ('500', 'bad')
        o = out.setdefault(code, {'code': code, 'kind': kind, 'types': [], 'new': False})
        if t not in o['types']:
            o['types'].append(t)
        o['new'] = o['new'] or origin in changed_q
    return sorted(out.values(), key=lambda o: (o['kind'] != 'ok', o['code']))


def build_endpoint(entry_qual, idx, meta, depth, tag, changed_q=frozenset(), changed_m=frozenset(),
                   diffs=None, used_by=None):
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
    flow, recv, facts = trace_values(order, entry_qual, idx, nid)
    nodes = []
    for q in order:
        info = idx.funcs[q]
        steps = []
        for s0 in steps_for(info, idx):
            s = {k: v for k, v in s0.items() if not k.startswith('_')}
            s['target'] = f"{tag}__{s['target']}"
            # errors the callee lets out: stopped by an `except` around this call, or passing through
            esc, caught = [], []
            for t, st, origin, text in escapes(s0['_qual'], idx):
                h = caught_by(t, s0.get('_catch', []), idx)
                if h:
                    caught.append([t, h[1] or ''])
                elif not st and app_status(t, idx) is None and t not in esc:
                    esc.append(t)
            if esc:
                s['esc'] = esc
            if caught:
                s['caught'] = [list(x) for x in dict.fromkeys(map(tuple, caught))]
            steps.append(s)
        n = {
            'id': nid(q), 'fnKey': fk(q), 'col': col[q], 'entry': q == entry_qual,
            'layer': idx.layer(q), 'changed': q in changed_q,
            'title': (f"{info['cls']}." if info['cls'] else '') + info['name'],
            'sub': f"{info['file'].split('/')[-1]}:{info['lineno']}", 'steps': steps,
            'locals': local_names(info['node']),
        }
        if flow.get(q):
            n['flow'] = flow[q]
        if recv.get(q):
            n['recv'] = recv[q]
        if facts[q]['reads']:
            n['reads'] = facts[q]['reads']
        rs = [{'t': r['t'], 'text': r['text'],
               'esc': not r['status'] and app_status(r['t'], idx) is None
                      and not caught_by(r['t'], r['_catch'], idx)}
              for r in idx.raise_cache.get(q, [])]
        if rs:
            n['raises'] = rs
        ub = (used_by or {}).get(q, [])
        if len(ub) > 1:
            n['usedBy'] = ub
        if q in diffs:
            n['base'] = diffs[q]['base']; n['diff'] = diffs[q]['diff']; n['isnew'] = diffs[q]['new']
        nodes.append(n)
    # rootless entry (no route reaches it): graft its callers as collapsed "ghost" nodes to the left,
    # so the flow is reconnected without pulling in each caller's whole tree. They're clickable/expandable.
    if not meta.get('route'):
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
                'layer': idx.layer(cq), 'changed': False,
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

    # only the entry's own parameters are inputs; deeper functions take their colour from the
    # value they were passed (trace_values), so a same-named but unrelated parameter isn't tinted
    uniq = list(dict.fromkeys(idx.funcs[entry_qual]['params']))[:len(INPUT_PALETTE)]
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
        'fieldUse': field_use(entry_qual, order, flow, facts, idx, nid),
        'statuses': statuses_for(entry_qual, idx, changed_q) if meta.get('route') else [],
    }


def _is_migration(rel):
    parts = rel.lower().split('/')
    return any(p in ('alembic', 'migrations') for p in parts[:-1]) and not parts[-1].startswith('__')


def migration_ops(src):
    """One line per schema operation in upgrade(), e.g. `op.add_column("users", sa.Column("email", …))`."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    fn = next((n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == 'upgrade'), None)
    if not fn:
        return []
    ops = []
    for n in ast.walk(fn):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and isinstance(n.func.value, ast.Name) and n.func.value.id in ('op', 'batch_op')):
            ops.append((n.lineno, one_line(unparse(n), 110)))
    return [o for _, o in sorted(ops)][:40]


def migration_entries(repo, tag, base, ref, changed_files, merge_base, code_out):
    """Changed Alembic/Django-style migration files: they are never called from a route (and are
    skipped when indexing), so without this a migration in the diff would be invisible. Each becomes
    one node: its upgrade() operations as rows, and the whole file as a diff."""
    import difflib
    mb = diff_base(repo, base, ref, merge_base)
    out = []
    for rel in sorted(f for f in changed_files if _is_migration(f)):
        head_src = git(repo, 'show', f'{ref}:{rel}') if ref else (
            open(os.path.join(repo, rel)).read() if os.path.isfile(os.path.join(repo, rel)) else '')
        base_src = git(repo, 'show', f'{mb}:{rel}')
        if head_src == base_src:
            continue
        deleted = not head_src
        src = head_src or base_src
        try:
            doc = ast.get_docstring(ast.parse(src)) or ''
        except SyntaxError:
            doc = ''
        name = os.path.basename(rel)[:-3]
        title = one_line(doc.split('\n')[0], 70) if doc else name
        diff = '\n'.join(difflib.unified_diff(base_src.splitlines(), head_src.splitlines(),
                                               fromfile=f'{rel} (base)', tofile=f'{rel} (branch)',
                                               lineterm='', n=3 if base_src and head_src else 100000))
        nid = f'{tag}__mig_{nid_for(rel)}'
        fkey = f'{tag}::mig:{rel}'
        code_out[fkey] = {'file': rel, 'lineno': 1, 'name': name, 'cls': None, 'code': head_src}
        node = {'id': nid, 'fnKey': fkey, 'col': 0, 'entry': True, 'layer': 'migration',
                'changed': True, 'deleted': deleted, 'isnew': not base_src, 'title': title,
                'sub': rel, 'steps': [], 'notes': migration_ops(src), 'base': base_src, 'diff': diff}
        out.append({'id': nid, 'group': f'{tag} · migrations', 'method': 'mig', 'other': True,
                    'path': name, 'title': title, 'summary': rel, 'inputs': [], 'nodes': [node],
                    'modelEdges': [], 'reqModels': [], 'respModels': [], 'models': {}})
    return out


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
    changed_q, changed_m, diffs, deleted, cf = set(), set(), {}, {}, set()
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

    # which routes reach each function (unbounded depth), for "used by N routes"
    route_reach = {q: reachable_quals(q, idx, 10 ** 6) for q, *_ in idx.routes}
    used_by = {}
    for q, m, p, pre in idx.routes:
        for f in route_reach[q]:
            used_by.setdefault(f, []).append(f'{m} {p}')
    build = lambda q, meta: build_endpoint(q, idx, meta, args.depth, tag, changed_q, changed_m, diffs,
                                           used_by)
    rendered_quals = lambda e: {n['fnKey'].split('::', 1)[1] for n in e['nodes']
                                if not n['fnKey'].split('::', 1)[1].startswith('model:')}
    del_code = {}  # code entries for synthetic DELETED nodes (no HEAD source to look up)

    if args.entries:
        endpoints = [build(q, {}) for name in args.entries
                     for q in idx.by_simple.get(name, []) + idx.by_method.get(name, [])]
    elif not changed:
        endpoints = [build(q, {'method': m, 'path': p, 'prefix': pre, 'route': True})
                     for q, m, p, pre in idx.routes]
    else:  # --changed: routes whose flow was changed somehow, then a coverage pass for the rest
        hit = [(q, m, p, pre) for q, m, p, pre in idx.routes if flow_touches_changed(q)[1]]
        endpoints = [build(q, {'method': m, 'path': p, 'prefix': pre, 'route': True})
                     for q, m, p, pre in hit]
        # edited vs only affected: a route is edited if its handler changed, or it reaches a changed
        # function no other route reaches. The rest only reach changed code shared with an edited
        # route; they go in their own group so a reviewer sees what else the change touches.
        hitq = {h[0] for h in hit}
        routes_of = lambda f: {r for r in hitq if f in route_reach[r]}
        edited = {r for r in hitq if r in changed_q
                  or any(routes_of(f) == {r} for f in route_reach[r] & changed_q)}
        if edited:
            for e, (q, *_rest) in zip(endpoints, hit):
                if q not in edited:
                    e['affected'] = True
                    e['group'] = f'{tag} · affected, not edited'
        # coverage guarantee: every changed function must appear as a node in ≥1 chart. Any changed
        # def not actually rendered (e.g. deeper than --depth, or reached only by non-route code)
        # becomes its own root chart, so nothing in the diff is invisible.
        shown = set().union(*(rendered_quals(e) for e in endpoints)) if endpoints else set()
        for q in sorted(changed_q - shown):
            if q not in idx.funcs or q in shown:
                continue
            kind = idx.layer(q)
            e = build(q, {'groupOverride': f'{tag} · not reached from a route',
                          'method': _KIND_BADGE.get(kind, 'fn')})
            e['other'] = True
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
                    'col': 0, 'entry': True, 'changed': True, 'title': name,
                    'layer': (lambda k: k if k in ('model', 'schema') else 'model')(layer_of(m['file'], idx.layers)),
                    'sub': f"{m['file'].split('/')[-1]}:{m['lineno']}", 'steps': []}
            d = diffs.get('model:' + name)
            if d:
                node['base'] = d['base']; node['diff'] = d['diff']; node['isnew'] = d['new']
            endpoints.append({
                'id': node['id'], 'group': f'{tag} · not reached from a route', 'method': 'class', 'other': True,
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
                    'deleted': True, 'layer': 'model' if dd['is_model'] else layer_of(dd['file'], idx.layers),
                    'title': dd['name'], 'sub': f"{dd['file'].split('/')[-1]}:{dd['lineno']}",
                    'steps': [], 'base': dd['base'], 'diff': dd['diff'], 'isnew': False}
            del_code[fkey] = {'file': dd['file'], 'lineno': dd['lineno'], 'name': dd['name'],
                              'cls': None, 'code': ''}
            endpoints.append({
                'id': nidd, 'group': f'{tag} · not reached from a route', 'method': 'del', 'other': True,
                'path': dd['name'], 'title': dd['name'], 'summary': 'deleted definition',
                'inputs': [], 'nodes': [node], 'modelEdges': [], 'reqModels': [], 'respModels': [],
                'models': {}})
        endpoints += migration_entries(repo, tag, base, ref, cf, not exact, del_code)
    code = dict(del_code)
    for e in endpoints:
        for n in e['nodes']:
            q = n['fnKey'].split('::', 1)[1]
            if q.startswith('del:') or q.startswith('mig:'):
                continue  # DELETED / migration node — code already provided by del_code
            if q.startswith('model:'):
                m = idx.models[q.split('model:', 1)[1]]
                code[n['fnKey']] = {'file': m['file'], 'lineno': m['lineno'],
                                    'name': m['name'], 'cls': None, 'code': m['code']}
            else:
                info = idx.funcs[q]
                code[n['fnKey']] = {'file': info['file'], 'lineno': info['lineno'],
                                    'name': info['name'], 'cls': info['cls'], 'code': info['code']}
    return endpoints, code


def list_refs(repo, limit=40):
    """Branches, tags and recent commits of `repo`, for the page's Compare picker."""
    def lines(*a):
        return [l for l in git(repo, *a).split('\n') if l]
    cur = git(repo, 'rev-parse', '--abbrev-ref', 'HEAD')
    heads = lines('for-each-ref', '--sort=-committerdate', '--format=%(refname:short)\t%(committerdate:relative)', 'refs/heads')
    remotes = lines('for-each-ref', '--sort=-committerdate', '--format=%(refname:short)\t%(committerdate:relative)', 'refs/remotes')
    tags = lines('for-each-ref', '--sort=-creatordate', '--format=%(refname:short)\t%(creatordate:relative)', 'refs/tags')
    commits = lines('log', f'-{limit}', '--format=%h\t%s\t%cr\t%an', 'HEAD')
    split = lambda rows, keys: [dict(zip(keys, r.split('\t'))) for r in rows]
    return {'current': cur,
            'branches': split(heads, ('name', 'when')),
            'remotes': [r for r in split(remotes, ('name', 'when')) if not r['name'].endswith('/HEAD')][:40],
            'tags': split(tags, ('name', 'when'))[:20],
            'commits': split(commits, ('sha', 'subject', 'when', 'author'))}


def ref_exists(repo, ref):
    return bool(ref) and not ref.startswith('-') and bool(
        git(repo, 'rev-parse', '--verify', '--quiet', f'{ref}^{{commit}}'))


def range_meta(args):
    """What this run shows, in the Compare picker's terms: mode all | branch | exact."""
    if args.commit_from:
        return {'mode': 'exact', 'base': args.commit_from, 'head': args.commit_to or ''}
    if args.changed:
        return {'mode': 'branch', 'base': args.base, 'head': args.branch or ''}
    return {'mode': 'all', 'base': args.base, 'head': args.branch or ''}


def generate(args, repos, log=True):
    all_eps, all_code = [], {}
    for repo in repos:
        if log:
            print(f'  scanning {os.path.basename(repo)}…', file=sys.stderr, flush=True)
        eps, code = process_repo(repo, args)
        all_eps += eps; all_code.update(code)
    # routes first, then the "not reached from a route" / migrations groups; same groups contiguous
    all_eps.sort(key=lambda e: (bool(e.get('other')), bool(e.get('affected')), e.get('group', '')))
    return all_eps, all_code


def render(args, repos, eps, code, serve=False):
    meta = dict(range_meta(args), repos=[os.path.basename(r) for r in repos], serve=serve,
                refs=list_refs(repos[0]) if repos else {})
    blob = json.dumps({'code': code, 'graph': {'endpoints': eps}, 'meta': meta}, ensure_ascii=False)
    blob = blob.replace('</', '<\\/')  # source text may contain "</script>"; keep it inside the JSON
    tpl = open(args.template).read()
    if '__DATA__' not in tpl:
        sys.exit('template missing __DATA__ placeholder')
    tpl = re.sub(r'<title>.*?</title>', lambda _: f'<title>{html.escape(args.title)}</title>', tpl, count=1)
    return tpl.replace('__DATA__', blob)


def serve(args, repos):
    """Local-only server so the page can switch what it compares: `/?mode=branch&base=main&head=feat`.
    Each range is generated on first request and cached; `&refresh=1` rebuilds it."""
    import http.server, socketserver, threading, urllib.parse, webbrowser
    cache, lock = {}, threading.Lock()

    def args_for(q):
        mode = q.get('mode', range_meta(args)['mode'])
        base = q.get('base', args.commit_from or args.base) or 'main'
        head = q.get('head', args.commit_to or args.branch or '') or ''
        for ref in (base, head):
            if ref and not any(ref_exists(r, ref) for r in repos):
                raise ValueError(f'unknown ref: {ref}')
        a = copy.copy(args)
        a.changed, a.branch, a.commit_from, a.commit_to, a.base = False, head or None, None, None, base
        if mode == 'branch':
            a.changed = True
        elif mode == 'exact':
            a.commit_from, a.commit_to, a.branch = base, head or None, None
        return a

    class H(http.server.BaseHTTPRequestHandler):
        def send(self, status, body, ctype='text/html; charset=utf-8'):
            data = body.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            # only answer to our own origin: stops a page on another site from reading your source
            # through DNS rebinding (its Host header would name that site, not 127.0.0.1)
            host = (self.headers.get('Host') or '').split(':')[0]
            if host not in ('127.0.0.1', 'localhost'):
                return self.send(403, 'forbidden', 'text/plain')
            u = urllib.parse.urlparse(self.path)
            q = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
            try:
                if u.path == '/api/refs':
                    return self.send(200, json.dumps(list_refs(repos[0])), 'application/json')
                if u.path != '/':
                    return self.send(404, 'not found', 'text/plain')
                a = args_for(q)
                key = (a.changed, a.base, a.branch, a.commit_from, a.commit_to)
                with lock:
                    if q.get('refresh') or key not in cache:
                        # the working tree moves under us, so only cache ranges between fixed refs
                        eps, code = generate(a, repos, log=False)
                        page = render(a, repos, eps, code, serve=True)
                        if a.branch or a.commit_to:
                            cache[key] = page
                    else:
                        page = cache[key]
                self.send(200, page)
            except ValueError as e:
                self.send(400, str(e), 'text/plain')

        def log_message(self, fmt, *a):
            print('  ' + (fmt % a), file=sys.stderr)

    class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    try:
        srv = Server(('127.0.0.1', args.port), H)
    except OSError:
        srv = Server(('127.0.0.1', 0), H)  # port taken: let the OS pick one
    url = f'http://127.0.0.1:{srv.server_address[1]}/'
    print(f'flowsegul serving {", ".join(os.path.basename(r) for r in repos)} at {url}  (Ctrl+C to stop)')
    if not args.no_open:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print()


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
    ap.add_argument('--serve', action='store_true',
                    help='run a local server instead of writing a file, so the page can switch '
                    'between branches, commits and ranges itself')
    ap.add_argument('--port', type=int, default=8765, help='port for --serve (default 8765)')
    ap.add_argument('--no-open', action='store_true', help='with --serve, do not open a browser')
    args = ap.parse_args()

    repos = args.repos or discover_repos(args.workspace or auto_workspace())
    repos = [os.path.abspath(r) for r in repos]
    if not repos:
        sys.exit('No Python repos found. Pass --repo DIR or run from your repos umbrella.')
    if args.serve:
        return serve(args, repos)

    all_eps, all_code = generate(args, repos)
    if not all_eps:
        hint = ' changed on this branch' if (args.changed or args.commit_from) else ''
        sys.exit(f'No entries found{hint} across: {", ".join(os.path.basename(r) for r in repos)}.')
    open(args.out, 'w').write(render(args, repos, all_eps, all_code))
    groups = {}
    for e in all_eps:
        groups[e['group']] = groups.get(e['group'], 0) + 1
    print(f'Wrote {args.out}')
    print('  ' + ' · '.join(f'{g}: {n}' for g, n in groups.items())
          + f'  ({len(all_code)} functions)')


if __name__ == '__main__':
    main()
