"""Request bodies and responses as JSON shapes, so a frontend request can be checked against the
FastAPI route it reaches: the fields it sends against the route's body model, and the type it expects
back against the route's response model.

A shape is the same on both sides (flowsegul_react.mjs writes the TypeScript one):
    'str' | 'num' | 'bool' | 'null' | 'any' | {'a': item} | {'u': [shapes]}
    | {'o': {key: [shape, optional, type text]}, 'n': name}
'any' is anything that can't be told (a dict, Decimal, a class from a library) and never gives a mark.
"""
import ast
import difflib
import re

STR = {'str', 'bytes', 'EmailStr', 'NameEmail', 'UUID', 'UUID1', 'UUID3', 'UUID4', 'UUID5', 'datetime',
       'date', 'time', 'timedelta', 'AnyUrl', 'AnyHttpUrl', 'HttpUrl', 'FileUrl', 'PostgresDsn', 'SecretStr',
       'IPv4Address', 'IPv6Address', 'IPvAnyAddress', 'Path', 'FilePath', 'DirectoryPath', 'constr',
       'AwareDatetime', 'NaiveDatetime', 'PastDate', 'FutureDate', 'PastDatetime', 'FutureDatetime',
       'StrictStr', 'Base64Str', 'Color'}
NUM = {'int', 'float', 'conint', 'confloat', 'PositiveInt', 'NegativeInt', 'NonNegativeInt', 'NonPositiveInt',
       'PositiveFloat', 'NegativeFloat', 'NonNegativeFloat', 'NonPositiveFloat', 'StrictInt', 'StrictFloat'}
BOOL = {'bool', 'StrictBool'}
LISTS = {'list', 'List', 'Sequence', 'MutableSequence', 'set', 'Set', 'frozenset', 'FrozenSet', 'Iterable',
         'Collection', 'conlist', 'conset'}
MODEL_BASES = {'BaseModel', 'SQLModel', 'BaseSettings', 'GenericModel', 'TypedDict'}
ENUM_BASES = {'Enum', 'StrEnum', 'IntEnum', 'Flag', 'IntFlag'}
PLAIN_BASES = {'object', 'Generic', 'ABC'}


def _kw(call, name):
    return next((k.value for k in call.keywords if k.arg == name), None)


def _const_str(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def union(parts):
    out = []
    for p in parts:
        for q in (p['u'] if isinstance(p, dict) and 'u' in p else [p]):
            if q == 'any':
                return 'any'
            if q not in out:
                out.append(q)
    return out[0] if len(out) == 1 else {'u': out} if out else 'any'


class Shapes:
    """Shapes of the repo's Pydantic models and annotations. `idx` is flowsegul_gen's Index."""

    def __init__(self, idx, unparse):
        self.idx, self.unparse = idx, unparse
        self.cache = {}

    def _last(self, node):
        return self.unparse(node).split('.')[-1]

    def kind(self, name, seen=None):
        """'model', 'str'/'num' for an Enum of those, 'enum' for another Enum, or None."""
        if name in MODEL_BASES:
            return 'model'
        seen = seen or set()
        node = self.idx.class_nodes.get(name)
        if node is None or name in seen:
            return None
        seen.add(name)
        if any(self._last(d) == 'dataclass' or (isinstance(d, ast.Call) and self._last(d.func) == 'dataclass')
               for d in node.decorator_list):
            return 'model'
        bases = [self._last(b.value if isinstance(b, ast.Subscript) else b) for b in node.bases]
        if any(b in ENUM_BASES for b in bases):
            return 'str' if 'str' in bases or 'StrEnum' in bases else 'num' if 'int' in bases or 'IntEnum' in bases else 'enum'
        for b in bases:
            k = self.kind(b, seen)
            if k:
                return k
        return None

    def _config(self, node):
        """(extra setting, has an alias generator) from `model_config = ConfigDict(…)` or `class Config`."""
        extra, gen = None, False
        for m in node.body:
            if isinstance(m, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'model_config' for t in m.targets):
                v = m.value
                pairs = [(k.arg, k.value) for k in v.keywords] if isinstance(v, ast.Call) else \
                    [(_const_str(k), val) for k, val in zip(v.keys, v.values)] if isinstance(v, ast.Dict) else []
                for k, val in pairs:
                    if k == 'extra':
                        extra = _const_str(val) or self._last(val).lower()
                    if k == 'alias_generator':
                        gen = True
            if isinstance(m, ast.ClassDef) and m.name == 'Config':
                for a in m.body:
                    if isinstance(a, ast.Assign) and isinstance(a.targets[0], ast.Name):
                        if a.targets[0].id == 'extra':
                            extra = _const_str(a.value) or self._last(a.value).lower()
                        if a.targets[0].id == 'alias_generator':
                            gen = True
        return extra, gen

    def model(self, name, out, depth=0):
        """A model's shape as JSON sees it: `out` for what the server sends (serialization aliases),
        otherwise what it accepts (validation aliases, and whether each field may be left out)."""
        key = (name, out)
        if key in self.cache:
            return self.cache[key]
        if depth > 4:
            return 'any'
        self.cache[key] = 'any'           # a model that contains itself
        node = self.idx.class_nodes[name]
        fields, open_, extra = {}, False, None
        for b in node.bases:
            bn = self._last(b.value if isinstance(b, ast.Subscript) else b)
            if bn in MODEL_BASES or bn in PLAIN_BASES:
                continue
            if bn in self.idx.class_nodes and self.kind(bn) == 'model':
                sup = self.model(bn, out, depth)
                if sup == 'any':
                    open_ = True
                    continue
                fields.update(sup['o'])
                open_ = open_ or bool(sup.get('open'))
                extra = sup.get('extra') or extra
            else:
                open_ = True               # a base we can't see may add fields
        ex, gen = self._config(node)
        extra = ex or extra
        for m in node.body:
            if not (isinstance(m, ast.AnnAssign) and isinstance(m.target, ast.Name)):
                continue
            nm = m.target.id
            if nm.startswith('_') or 'ClassVar' in self.unparse(m.annotation) or nm == 'model_config':
                continue
            val, has_default, k = m.value, m.value is not None, nm
            if isinstance(val, ast.Call):
                fn = self._last(val.func)
                if fn in ('Relationship', 'PrivateAttr'):
                    continue               # not part of the JSON
                if fn == 'Field':
                    first = val.args[0] if val.args else _kw(val, 'default')
                    has_default = (first is not None and not (isinstance(first, ast.Constant) and first.value is Ellipsis)) \
                        or _kw(val, 'default_factory') is not None
                    if _kw(val, 'exclude') is not None and out:
                        continue
                    alias = _const_str(_kw(val, 'serialization_alias' if out else 'validation_alias')) or _const_str(_kw(val, 'alias'))
                    k = alias or nm
            fields.pop(nm, None)
            fields[k] = [self.ann(m.annotation, depth + 1, out), 0 if out else int(has_default),
                         self.unparse(m.annotation)]
        m = self.idx.models.get(name) or {}
        sh = {'o': fields, 'n': name, 'at': '%s:%s' % (m.get('file', '?'), m.get('lineno', '?'))}
        if open_:
            sh['open'] = 1        # a base we can't see may add fields
        if gen:
            sh['alias'] = 1       # an alias generator renames every key: can't match keys at all
        if extra:
            sh['extra'] = extra
        self.cache[key] = sh
        return sh

    def ann(self, node, depth=0, out=True):
        """The shape of an annotation (an ast node or its text)."""
        if isinstance(node, str):
            try:
                node = ast.parse(node.strip(), mode='eval').body
            except SyntaxError:
                return 'any'
        if node is None or depth > 6:
            return 'any'
        if isinstance(node, ast.Constant):
            if node.value is None:
                return 'null'
            if isinstance(node.value, str):          # "Item": a forward reference
                return self.ann(node.value, depth, out)
            return 'any'
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return union([self.ann(node.left, depth, out), self.ann(node.right, depth, out)])
        if isinstance(node, (ast.Name, ast.Attribute)):
            n = self._last(node)
            if n in ('None', 'NoneType'):
                return 'null'
            if n in STR:
                return 'str'
            if n in NUM:
                return 'num'
            if n in BOOL:
                return 'bool'
            k = self.kind(n) if n in self.idx.class_nodes else None
            if k == 'model':
                return self.model(n, out, depth)
            if k in ('str', 'num'):
                return k
            return 'any'
        if isinstance(node, ast.Subscript):
            base = self._last(node.value)
            sl = node.slice
            if isinstance(sl, ast.Index):                # Python 3.8
                sl = sl.value
            elts = list(sl.elts) if isinstance(sl, ast.Tuple) else [sl]
            if base == 'Optional':
                return union([self.ann(elts[0], depth, out), 'null'])
            if base == 'Union':
                return union([self.ann(e, depth, out) for e in elts])
            if base == 'Annotated':
                return self.ann(elts[0], depth, out)
            if base in LISTS:
                return {'a': self.ann(elts[0], depth + 1, out)}
            if base in ('tuple', 'Tuple') and len(elts) == 2 and isinstance(elts[1], ast.Constant) and elts[1].value is Ellipsis:
                return {'a': self.ann(elts[0], depth + 1, out)}
            if base == 'Literal':
                kinds = []
                for e in elts:
                    v = e.value if isinstance(e, ast.Constant) else object()
                    kinds.append('null' if v is None else 'bool' if isinstance(v, bool) else 'str' if isinstance(v, str)
                                 else 'num' if isinstance(v, (int, float)) else 'any')
                return union(kinds)
        return 'any'


# ── checking a request against its route ──
TS_WORD = {'str': 'string', 'num': 'number', 'bool': 'boolean', 'null': 'null'}


def _kinds(sh):
    parts = sh['u'] if isinstance(sh, dict) and 'u' in sh else [sh]
    return parts


def _core(parts):
    """What a value is apart from null: 'any', a scalar name, 'a', 'o', or None when it's a mix."""
    ks = set()
    for p in parts:
        if p == 'null':
            continue
        ks.add('a' if isinstance(p, dict) and 'a' in p else 'o' if isinstance(p, dict) else p)
    if 'any' in ks:
        return 'any'
    return ks


def _pick(parts, k):
    return next((p for p in parts if isinstance(p, dict) and k in p), None)


class Check:
    """Where one side of a request doesn't fit what the other side declares. Each issue is
    (tone, frontend word, Python field, Python model it belongs to, note): tone 'bad' is red,
    'dim' grey (maybe); the frontend word is what to underline in the call's code ('' when the
    frontend has nothing there); the note is a short code comment to put on the Python field's
    line. `got` is the value that arrives, `want` what the receiving side declares."""

    def __init__(self, lax, sending):
        self.lax = lax            # TypeScript without strict null checks: every type may be null
        self.sending = sending    # the frontend sends a body, rather than reading a response
        self.issues = []
        self.verb = 'sends' if sending else 'reads'

    def add(self, tone, word, key, model, note):
        it = (tone, word, key, model.get('n') if model else '', note)
        if it not in [x[:5] for x in self.issues]:
            self.issues.append(it + (model,))

    def value(self, got, want, owner, key, gtext, wtext, depth=0):
        if depth > 5 or got == 'any' or want == 'any':
            return
        gp, wp = _kinds(got), _kinds(want)
        gk, wk = _core(gp), _core(wp)
        if gk == 'any' or wk == 'any':
            return
        ts_ = gtext if self.sending else wtext
        if gk and wk and not (gk & wk):
            # the body's "3" still becomes 3: Pydantic reads numbers and booleans from strings
            soft = self.sending and gk == {'str'} and wk <= {'num', 'bool'}
            self.add('dim' if soft else 'bad', key, key, owner, 'frontend %s %s' % (self.verb, ts_ or '?'))
            return
        # null where the other side doesn't allow it
        if 'null' in gp and 'null' not in wp and not self.lax:
            note = 'frontend may send null' if self.sending else 'frontend reads %s' % (ts_ or '?')
            self.add('bad' if not gk else 'dim', key, key, owner, note)
        if len(gk) != 1 or gk != wk:
            return
        k = next(iter(gk))
        if k == 'a':
            self.value(_pick(gp, 'a')['a'], _pick(wp, 'a')['a'], owner, key, '', '', depth + 1)
        elif k == 'o':
            if len([p for p in gp if isinstance(p, dict) and 'o' in p]) > 1 or len([p for p in wp if isinstance(p, dict) and 'o' in p]) > 1:
                return             # a union of objects: can't tell which one
            self.fields(_pick(gp, 'o'), _pick(wp, 'o'), depth + 1)

    def fields(self, g, w, depth):
        if g.get('alias') or w.get('alias'):
            return
        sending = self.sending
        py = w if sending else g
        only_ts, only_py = [], []       # (key, tone): a key one side has and the other doesn't
        if sending:
            # keys the frontend sends that the model doesn't have: dropped, or a 422 if extra is forbidden
            if not w.get('open') and w.get('extra') != 'allow':
                only_ts = [(k, 'dim' if g['o'][k][1] else 'bad') for k in g['o'] if k not in w['o']]
            for k, (sh, opt, txt) in w['o'].items():
                if opt:
                    continue
                if k not in g['o']:
                    only_py.append((k, 'bad'))
                elif g['o'][k][1]:
                    self.add('dim', k, k, py, 'may not be sent')    # optional in TypeScript, required here
        elif not g.get('open'):
            # fields the frontend counts on that the response never has
            only_ts = [(k, 'bad') for k, (sh, opt, txt) in w['o'].items() if not opt and k not in g['o']]
            only_py = [(k, 'pair') for k in g['o'] if k not in w['o']]   # only used to spot a rename
        # a key on one side and a similar one on the other is most likely a rename
        for k, tone in only_ts:
            m = next((x for x in only_py if _similar(k, x[0])), None)
            if m:
                only_py.remove(m)
            self.add(tone, k, m[0] if m else '', py, 'frontend %s %s' % (self.verb, k))
        for k, tone in only_py:
            if tone != 'pair':
                self.add(tone, '', k, py, 'not sent')
        for k in g['o']:
            if k in w['o']:
                gs, _, gt = g['o'][k]
                ws, _, wt = w['o'][k]
                self.value(gs, ws, py, k, gt, wt, depth)


def _similar(a, b):
    a, b = a.lower().replace('_', ''), b.lower().replace('_', '')
    return a.startswith(b) or b.startswith(a) or difflib.SequenceMatcher(None, a, b).ratio() >= 0.5


def ts_text(sh):
    if isinstance(sh, str):
        return TS_WORD.get(sh, sh)
    if 'a' in sh:
        return ts_text(sh['a']) + '[]'
    if 'u' in sh:
        return ' | '.join(ts_text(x) for x in sh['u'])
    return sh.get('n') or 'object'


def _lines(model, issues, src):
    """The Python model's own lines, each problem field's line with its note as a comment."""
    code = (src or {}).get(model['n']) if model else None
    if not code:
        head = ('class %s' % model['n']) if model else ''
        return [[head, '']] + [['    # ' + it[4], it[0]] for it in issues]
    lines = [[ln.rstrip(), ''] for ln in code.rstrip().split('\n') if not ln.lstrip().startswith('@')]
    extra, keep = [], {0}
    for tone, word, key, _, note, _m in issues:
        at = next((i for i, (t, _) in enumerate(lines) if key and re.match(r'\s+%s\s*[:=]' % re.escape(key), t)), None)
        if not key and not word:
            at = 0               # about the model as a whole: on its `class` line
        if at is None:
            extra.append(['    # ' + note, tone])
            continue
        t, old = lines[at]
        lines[at] = [t + ('  # ' if '#' not in t else ', ') + note, 'bad' if 'bad' in (tone, old) else tone]
        keep.add(at)
    if len(lines) > 14:          # a long model: its first line and the lines that matter
        out, gap = [], False
        for i, l in enumerate(lines):
            if i in keep or i < 3:
                out.append(l)
                gap = False
            elif not gap:
                out.append(['    ...', ''])
                gap = True
        lines = out
    return lines + extra


def check_request(h, route, lax=False):
    """Where request `h` (from flowsegul_react.mjs) doesn't fit `route` (route_facts):
    [{'k': 'sends' | 'reads', 'tone', 'keys': frontend keys and 'name' its type, to underline, 'models':
    [{'n', 'at', 'lines': [[text, tone]]}]}], only where something doesn't fit."""
    lax = lax or bool(h.get('lax'))
    src = route.get('src')
    out = []

    def report(k, c, ts_name):
        if not c.issues:
            return
        models, order = {}, []
        for it in c.issues:
            key = it[3] or ''
            if key not in models:
                models[key] = (it[5], [])
                order.append(key)
            models[key][1].append(it)
        keys = list(dict.fromkeys(it[1] for it in c.issues if it[1]))
        name = ts_name if re.match(r'^\w+$', ts_name or '') and ts_name not in TS_WORD.values() else ''
        out.append({'k': k, 'tone': 'bad' if any(it[0] == 'bad' for it in c.issues) else 'dim', 'keys': keys, 'name': name,
                    'models': [{'n': models[n][0]['n'] if models[n][0] else n, 'at': (models[n][0] or {}).get('at', ''),
                                'lines': _lines(models[n][0], models[n][1], src)} for n in order]})

    obj = lambda sh: sh if isinstance(sh, dict) and 'o' in sh else None
    body = route.get('body')
    if body and not body.get('form') and not h.get('bx'):
        c = Check(lax, True)
        if h.get('nb'):
            if body.get('req'):
                m = obj(body['s']) or {'n': body.get('t') or 'body'}
                c.add('bad', '', '', m, 'the request sends no body')
        elif h.get('b'):
            b = h['b']
            top = obj(body['s']) or {'n': body.get('t') or ''}
            c.value(b['s'], body['s'], top, '', b.get('t') or ts_text(b['s']), body.get('t') or '')
        report('sends', c, (h.get('b') or {}).get('t') or '')
    resp = route.get('resp')
    r = h.get('r')
    if resp and r:
        c = Check(lax, False)
        top = obj(resp['s']) or {'n': resp.get('t') or ''}
        ts_name = r.get('t') or ts_text(r['s'])
        c.value(resp['s'], r['s'], top, '', resp.get('t') or '', ts_name)
        report('reads', c, ts_name)
    return out
