"""Mismatched types where a value is handed from one function to another.

flowsegul reads the annotations it can see: a parameter's type, a function's `-> type`, schema
fields, and the values some well-known calls return (`os.getenv` gives `str | None`, `.encode()`
gives bytes, `db.get(User, id)` gives `User | None`). A value whose type can't be one the receiving
end takes is a finding:

  * an argument vs the parameter it lands in:   `str | None ≠ str`, `str ≠ UUID`, `bytes ≠ str`
  * what a callee returns vs what the caller says it returns (`return repo.get(id)`)
  * a `… | None` value used without a check first (`customer.name`, `return customer`)
  * bytes where text is expected: an f-string, `==` against text, `str(b)`, `b + "text"`

A `None` that an `if`/`assert` before the use may have ruled out, but not clearly, is uncertain
(drawn grey, not red). Unknown types are never guessed at: no finding.
"""
import ast, json, os, re, shutil, subprocess, sys

NONE = 'None'
_ALIAS = {'List': 'list', 'Dict': 'dict', 'Set': 'set', 'FrozenSet': 'frozenset', 'Tuple': 'tuple',
          'Text': 'str', 'NoneType': NONE}
# types whose values are plainly told apart: a str is never a UUID, a bytes never a str
_CONCRETE = {'str', 'bytes', 'bytearray', 'int', 'float', 'complex', 'bool', 'UUID', 'dict', 'list',
             'set', 'frozenset', 'tuple', 'datetime', 'date', 'time', 'timedelta', 'Decimal'}
_PROMOTE = {('int', 'float'), ('int', 'complex'), ('float', 'complex'), ('bool', 'int'),
            ('bool', 'float'), ('bytearray', 'bytes'), ('datetime', 'date')}
_BYTES_FUNCS = {'b64encode', 'b64decode', 'urlsafe_b64encode', 'urlsafe_b64decode', 'b32encode',
                'b32decode', 'b16encode', 'b16decode', 'digest', 'encode', 'to_bytes', 'tobytes'}
_STR_FUNCS = {'decode', 'hexdigest', 'hex', 'isoformat', 'strftime'}
_SAME_FUNCS = {'strip', 'lstrip', 'rstrip', 'lower', 'upper', 'title', 'replace', 'removeprefix',
               'removesuffix', 'zfill', 'casefold', 'capitalize'}
_MAYBE_NONE = {'first', 'one_or_none', 'scalar_one_or_none', 'scalar', 'fetchone'}
_TEXT_MAPS = {'headers', 'query_params', 'cookies', 'path_params', 'environ', 'args', 'form'}


# ---------------------------------------------------------------- type text ----------------------

def members(text):
    """Union members of a type annotation, by base name: 'Optional[Customer]' -> ['Customer',
    'None'], 'uuid.UUID | None' -> ['UUID', 'None'], 'list[int]' -> ['list']. '?' = unknown."""
    if not text:
        return []
    try:
        node = ast.parse(text.strip(), mode='eval').body
    except SyntaxError:
        return ['?']
    out = []
    for m in _members(node):
        if m not in out:
            out.append(m)
    return out


def _members(n):
    if isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr):
        return _members(n.left) + _members(n.right)
    if isinstance(n, ast.Constant):
        if n.value is None:
            return [NONE]
        if isinstance(n.value, str):          # a forward reference: 'Customer'
            return members(n.value)
        return ['?']
    if isinstance(n, ast.Subscript):
        base = _base(n.value)
        sl = n.slice.value if isinstance(n.slice, getattr(ast, "Index", ())) else n.slice   # 3.8 wraps it
        elts = sl.elts if isinstance(sl, ast.Tuple) else [sl]
        if base == 'Optional':
            return _members(elts[0]) + [NONE]
        if base == 'Union':
            return [m for e in elts for m in _members(e)]
        if base == 'Annotated':
            return _members(elts[0])
        if base in ('Literal', 'Type', 'type', 'ClassVar', 'Final'):
            return ['?']
        return [base]
    if isinstance(n, (ast.Name, ast.Attribute)):
        return [_base(n)]
    return ['?']


def _base(n):
    name = n.id if isinstance(n, ast.Name) else n.attr if isinstance(n, ast.Attribute) else '?'
    return _ALIAS.get(name, name)


def pretty(text):
    """The annotation as a reader would write it today: Optional[X] -> 'X | None', uuid.UUID -> UUID."""
    try:
        node = ast.parse(text.strip(), mode='eval').body
    except SyntaxError:
        return text
    return _pretty(node)


def _pretty(n):
    if isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr):
        return _pretty(n.left) + ' | ' + _pretty(n.right)
    if isinstance(n, ast.Constant):
        if n.value is None:
            return NONE
        return pretty(n.value) if isinstance(n.value, str) else repr(n.value)
    if isinstance(n, ast.Subscript):
        base = _base(n.value) if isinstance(n.value, ast.Attribute) else ast.unparse(n.value)
        sl = n.slice.value if isinstance(n.slice, getattr(ast, "Index", ())) else n.slice
        elts = sl.elts if isinstance(sl, ast.Tuple) else [sl]
        if base == 'Optional':
            return _pretty(elts[0]) + ' | None'
        if base == 'Union':
            return ' | '.join(_pretty(e) for e in elts)
        if base == 'Annotated':
            return _pretty(elts[0])
        return base + '[' + ', '.join(_pretty(e) for e in elts) + ']'
    if isinstance(n, ast.Attribute):
        return n.attr
    return ast.unparse(n)


def join_types(parts):
    """Union of type texts, None last: ['str', 'None', 'str'] -> 'str | None'."""
    seen = []
    for p in parts:
        for m in split_top(p):
            if m not in seen:
                seen.append(m)
    seen.sort(key=lambda m: m == NONE)
    return ' | '.join(seen)


def split_top(text):
    """'str | None' -> ['str', 'None']; keeps 'dict[str, int]' whole."""
    t = pretty(text)
    out, depth, cur = [], 0, ''
    for ch in t:
        depth += ch in '[('
        depth -= ch in '])'
        if ch == '|' and depth == 0:
            out.append(cur.strip()); cur = ''
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def without_none(text):
    return join_types([m for m in split_top(text) if m != NONE]) or text


class Checker:
    """What a type annotation can be given, as far as the repo's own classes say."""
    def __init__(self, idx):
        self.idx = idx
        tv = set()
        for vals in idx.module_values.values():
            for name, v in vals.items():
                if isinstance(v, ast.Call) and ast.unparse(v.func).split('.')[-1] in ('TypeVar', 'ParamSpec',
                                                                                       'NewType'):
                    tv.add(name)
        self.typevars = tv
        self.protocols = {c for c, bs in idx.bases.items()
                          if any(b in ('Protocol', 'TypedDict', 'NamedTuple', 'Generic') for b in bs)}

    def known(self, m):
        return (m in _CONCRETE or m in self.idx.bases) and m not in self.protocols

    def open_ended(self, m):
        """A target member anything might satisfy: Any, object, a TypeVar, a Protocol."""
        return m in ('Any', 'object', '?') or m in self.typevars or m in self.protocols

    def fits(self, s, t):
        return s == t or (s, t) in _PROMOTE or self.idx.is_sub(s, t)

    def look_alike(self, s, t):
        """Two of the repo's classes for the same thing, like an ORM row and its schema (Source /
        SourceRead): handing one for the other often works, attribute by attribute."""
        if s not in self.idx.bases or t not in self.idx.bases:
            return False
        def stem(c):
            while True:
                d = re.sub(r'(Read|Create|Update|Base|Out|In|Public|Schema|Model|DB|Db|Response|Request|'
                           r'Row|Record|Table|Entity|Dto|DTO|Pagination)$', '', c)
                if d == c or not d:
                    return c
                c = d
        return stem(s) == stem(t)

    def bad(self, got, want):
        """Members of `got` that `want` can't take ([] = fine or can't tell)."""
        gm, wm = members(got), members(want)
        if not gm or not wm or any(self.open_ended(t) for t in wm):
            return []
        all_known = all(self.known(t) or t == NONE for t in wm)
        out = []
        for s in gm:
            if s == NONE:
                if NONE not in wm:
                    out.append(s)
            elif all_known and self.known(s) and not any(self.fits(s, t) for t in wm if t != NONE):
                out.append(s)
        return out


# ---------------------------------------------------------------- one function -------------------

def _walk_own(fn):
    """Every node of a function body, but not of functions/lambdas/classes nested in it."""
    stack = list(fn.body)
    while stack:
        n = stack.pop()
        yield n
        for ch in ast.iter_child_nodes(n):
            if not isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                stack.append(ch)


def chain_text(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return '.'.join([node.id] + parts[::-1])
    return None


def ann_params(fn):
    """[(name, annotation text or '', default node or None)] in call order, self/cls left out."""
    a = fn.args
    pos = [*getattr(a, 'posonlyargs', []), *a.args]
    defaults = [None] * (len(pos) - len(a.defaults)) + list(a.defaults)
    out = [(x.arg, ast.unparse(x.annotation) if x.annotation is not None else '', d)
           for x, d in zip(pos, defaults)]
    out += [(x.arg, ast.unparse(x.annotation) if x.annotation is not None else '', d)
            for x, d in zip(a.kwonlyargs, a.kw_defaults)]
    # `x: dict = None` takes a None in practice (the old implicit Optional)
    out = [(n, f'{a} | None' if a and isinstance(d, ast.Constant) and d.value is None and NONE not in members(a)
            else a, d) for n, a, d in out]
    return [p for p in out if p[0] not in ('self', 'cls')]


class Scope:
    """Types of the values one function holds, by line, plus the checks guarding them."""
    def __init__(self, info, idx):
        self.info, self.idx, self.fn = info, idx, info['node']
        self.rel = info['file']
        self.recs = {}    # name -> [(lineno, value node | None, annotation text | None, mode)]
        for name, ann, dflt in ann_params(self.fn):
            implicit = ann.endswith(' | None') and isinstance(dflt, ast.Constant) and dflt.value is None \
                and not any(a.arg == name and NONE in members(ast.unparse(a.annotation))
                            for a in [*self.fn.args.args, *self.fn.args.kwonlyargs, *getattr(self.fn.args, 'posonlyargs', [])]
                            if a.annotation is not None)
            self.recs.setdefault(name, []).append((self.fn.lineno, None, ann or None, 'implicit' if implicit else None))
        self.parent = {}
        for n in _walk_own(self.fn):
            for ch in ast.iter_child_nodes(n):
                self.parent[ch] = n
            self._record(n)
        for v in self.recs.values():
            v.sort(key=lambda r: r[0])
        self.guards = self._guards()

    def _record(self, n):
        def unknown(t):
            for x in ast.walk(t):
                if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store):
                    self.recs.setdefault(x.id, []).append((x.lineno, None, None, None))
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    self.recs.setdefault(t.id, []).append((n.lineno, n.value, None, None))
                else:
                    unknown(t)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            self.recs.setdefault(n.target.id, []).append((n.lineno, None, ast.unparse(n.annotation), None))
        elif isinstance(n, ast.NamedExpr):
            self.recs.setdefault(n.target.id, []).append((n.lineno, n.value, None, None))
        elif isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension)):
            unknown(n.target)
        elif isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name):
            self.recs.setdefault(n.target.id, []).append((n.lineno, None, None, None))
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            for it in n.items:
                if isinstance(it.optional_vars, ast.Name):
                    mode = _open_mode(it.context_expr)
                    self.recs.setdefault(it.optional_vars.id, []).append(
                        (n.lineno, None, None, ('file', mode)) if mode is not None
                        else (n.lineno, it.context_expr, None, None))
                elif it.optional_vars is not None:
                    unknown(it.optional_vars)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            self.recs.setdefault(n.name, []).append((n.lineno, None, None, None))

    def rec(self, name, line):
        """The assignment of `name` in effect at `line` (the latest one before it)."""
        best = None
        for r in self.recs.get(name, []):
            if r[0] < line or (r[0] == self.fn.lineno and r[2] is not None):
                best = r
        return best

    # -- what type an expression has ---------------------------------------------------------
    def type_of(self, node, line, depth=0):
        """(type text, uncertain) or None when flowsegul can't tell."""
        if depth > 8 or node is None:
            return None
        d = depth + 1
        if isinstance(node, ast.Constant):
            if node.value is None:
                return (NONE, False)
            if isinstance(node.value, bool):
                return ('bool', False)
            return (type(node.value).__name__, False) if isinstance(node.value, (str, bytes, int, float)) else None
        if isinstance(node, ast.JoinedStr):
            return ('str', False)
        if isinstance(node, ast.Await):
            return self._call_type(node.value, line, d, awaited=True) if isinstance(node.value, ast.Call) \
                else self.type_of(node.value, line, d)
        if isinstance(node, ast.Name):
            r = self.rec(node.id, line)
            if r is None:
                t = self.idx.module_types.get(self.rel, {}).get(node.id)
                return (t, False) if t and t[0].isupper() and t in self.idx.bases else None
            if r[3] == 'implicit':   # `x: str = None`: None only when a caller leaves it out
                return (r[2], True)
            if r[3] and r[3][0] == 'file':
                return ('file:' + r[3][1], False)
            if r[2] is not None:
                return (r[2], False)
            return self.type_of(r[1], r[0], d) if r[1] is not None else None
        if isinstance(node, ast.Attribute):
            return self._attr_type(node, line, d)
        if isinstance(node, ast.Call):
            return self._call_type(node, line, d)
        if isinstance(node, ast.IfExp):
            a, b = self.type_of(node.body, line, d), self.type_of(node.orelse, line, d)
            if a and b:
                return (join_types([a[0], b[0]]), a[1] or b[1])
            return None
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            parts, dim = [], False
            for i, v in enumerate(node.values):
                t = self.type_of(v, line, d)
                if t is None:
                    return None
                parts.append(t[0] if i == len(node.values) - 1 else without_none(t[0]))
                dim = dim or t[1]
            return (join_types(parts), dim)
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
            a = self.type_of(node.left, line, d)
            if a and a[0] in ('str', 'bytes'):
                return a
        return None

    def _attr_type(self, node, line, d):
        if isinstance(node.value, ast.Name) and node.value.id == 'self' and self.info.get('cls'):
            for c in [self.info['cls']] + self.idx.bases.get(self.info['cls'], []):
                for _, attrs in self.idx.attr_types.get(c, []):
                    if node.attr in attrs and attrs[node.attr]:
                        t = attrs[node.attr]
                        return (t, False) if members(t) and members(t)[0] != '?' else None
            return None
        base = self.type_of(node.value, line, d)
        if not base:
            return None
        ms = [m for m in members(base[0]) if m != NONE]
        if len(ms) == 1 and ms[0] in self.idx.models:
            for f in self.idx.models[ms[0]].get('fields', []):
                if f['name'] == node.attr:
                    return (f['type'], base[1])
        return None

    def _call_type(self, call, line, d, awaited=False):
        fn = call.func
        name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ''
        q = self.idx.resolve(call, self.info.get('cls'), self.info['qual'])
        if q:
            callee = self.idx.funcs[q]
            if isinstance(callee['node'], ast.AsyncFunctionDef) and not awaited:
                return None                     # a coroutine, not the value
            return (callee['returns'], not self.sure(call)) if callee['returns'] else None
        if isinstance(fn, ast.Name):
            simple = {'str': 'str', 'repr': 'str', 'int': 'int', 'len': 'int', 'float': 'float',
                      'bool': 'bool', 'bytes': 'bytes', 'bytearray': 'bytearray', 'UUID': 'UUID',
                      'uuid4': 'UUID', 'uuid1': 'UUID', 'getenv': 'str | None'}
            if name == 'getenv':
                return self._getenv(call, line, d)
            if name in simple:
                return (simple[name], False)
            if name in self.idx.bases and name in self.idx.models and not self.idx.funcs.get(name):
                return (name, False)            # constructing one of our classes
            return None
        if not isinstance(fn, ast.Attribute):
            return None
        recv = fn.value
        rtxt = chain_text(recv) or ''
        if name == 'getenv' and rtxt == 'os':
            return self._getenv(call, line, d)
        if name == 'get' and rtxt.split('.')[-1] in _TEXT_MAPS and len(call.args) == 1 and not call.keywords:
            return ('str | None', False)        # request.headers.get("x"), os.environ.get("X")
        if name == 'get' and call.args and isinstance(call.args[0], ast.Name) \
                and call.args[0].id in self.idx.models and self._is_session(recv, line, d):
            return (call.args[0].id + ' | None', False)   # session.get(User, user_id)
        if name in ('uuid4', 'uuid1', 'UUID') and rtxt == 'uuid':
            return ('UUID', False)
        if name in ('encode', 'decode'):
            # str.encode / bytes.decode take at most an encoding; jwt.encode(payload, key) is not one
            recv_t = self.type_of(recv, line, d)
            text_like = recv_t and members(recv_t[0]) in (['str'], ['bytes'], ['bytearray'])
            if text_like or (len(call.args) <= 1 and all(isinstance(a, ast.Constant) for a in call.args)
                             and not call.keywords or [k.arg for k in call.keywords] in (['encoding'], ['errors'])):
                return ('bytes' if name == 'encode' else 'str', not text_like)
            return None
        if name in _BYTES_FUNCS:
            return ('bytes', False)
        if name in _STR_FUNCS:
            return ('str', False)
        if name in _MAYBE_NONE and not call.args:
            return ('? | None', True) if name in ('scalar', 'fetchone') else ('? | None', False)
        recv_t = self.type_of(recv, line, d)
        if name in _SAME_FUNCS and recv_t and recv_t[0] in ('str', 'bytes'):
            return recv_t
        if name == 'read' and recv_t:
            if recv_t[0].startswith('file:'):
                return ('bytes' if 'b' in recv_t[0][5:] else 'str', False)
            if members(recv_t[0])[:1] == ['UploadFile'] and awaited:
                return ('bytes', False)
        if name == 'body' and recv_t and members(recv_t[0])[:1] == ['Request'] and awaited:
            return ('bytes', False)
        return None

    def sure(self, call):
        """Is the function this call goes to known, not guessed from its name? `self.x()`, `f()`,
        `module.f()` and a receiver of a known class are; `thing.get()` on an unknown `thing` is not."""
        fn = call.func
        if not isinstance(fn, ast.Attribute):
            return True
        recv = fn.value
        if isinstance(recv, ast.Name) and recv.id in ('self', 'cls'):
            return True
        if isinstance(recv, ast.Call) and getattr(recv.func, 'id', '') == 'super':
            return True
        if self.idx.receiver_class(recv, self.info.get('cls'), self.info['qual']):
            return True
        if isinstance(recv, ast.Name):
            imp = self.idx.imports.get(self.rel, {}).get(recv.id)
            if imp:   # `from app import user_service` / `import app.users as users`
                mod = '.'.join(x for x in imp[:2] if x)
                return bool(self.idx.module_file(self.rel, mod, imp[2]) or self.idx.module_file(self.rel, imp[0], imp[2]))
        return False

    def _getenv(self, call, line, d):
        if len(call.args) >= 2:
            dflt = self.type_of(call.args[1], line, d)
            return ('str | ' + dflt[0], dflt[1]) if dflt else None
        return ('str | None', False)

    def _is_session(self, recv, line, d):
        t = self.type_of(recv, line, d)
        if t:
            return any('Session' in m for m in members(t[0]))
        return (chain_text(recv) or '').split('.')[-1] in ('db', 'session', 'sess', 'db_session', 'dbsession')

    # -- has a None been ruled out before this line? --------------------------------------------
    def _guards(self):
        out = []   # (start, end, tested chains, leaves the block, encloses only)
        for n in _walk_own(self.fn):
            if isinstance(n, (ast.If, ast.While)):
                out.append((n.lineno, n.end_lineno, _tested(n.test), _stops(n.body) or _stops(n.orelse), False))
            elif isinstance(n, ast.Assert):
                out.append((n.lineno, self.fn.end_lineno, _tested(n.test), True, False))
            elif isinstance(n, ast.IfExp):
                out.append((n.lineno, n.end_lineno, _tested(n.test), False, True))
            elif isinstance(n, ast.BoolOp):
                out.append((n.lineno, n.end_lineno, set().union(*[_tested(v) for v in n.values[:-1]]),
                            False, True))
            elif isinstance(n, ast.comprehension):
                for t in n.ifs:
                    out.append((t.lineno, t.end_lineno, _tested(t), False, True))
            elif isinstance(n, ast.Try):
                caught = {ast.unparse(h.type).split('.')[-1] if h.type is not None else 'Exception'
                          for h in n.handlers}
                if caught & {'AttributeError', 'TypeError', 'Exception', 'BaseException'}:
                    out.append((n.lineno, n.body[-1].end_lineno, {'*'}, False, True))
        return out

    def checked(self, text, line, since):
        """'yes' when an if/assert that tests `text` clearly rules None out at `line`, 'maybe' when
        something tests it but might not, else 'no'. Only checks after `since` (its assignment)."""
        best = 'no'
        for start, end, tested, stops, encloses_only in self.guards:
            if start < since:
                continue
            anything = '*' in tested
            if not (anything or text in tested or any(m.startswith(text + '.') for m in tested)):
                continue
            inside = start <= line <= end
            if anything:
                if inside:
                    best = 'maybe'
                continue
            if inside or (start < line and stops):
                return 'yes'
            if start < line and not encloses_only:
                best = 'maybe'
        return best

    def since(self, expr, line):
        root = expr
        while isinstance(root, ast.Attribute):
            root = root.value
        if isinstance(root, ast.Name):
            r = self.rec(root.id, line)
            return r[0] if r else 0
        return line

    def seg(self, node):
        src = self.idx.file_src.get(self.rel, '')
        try:
            s = ast.get_source_segment(src, node)
        except Exception:
            s = None
        return re.sub(r'\s+', ' ', s or ast.unparse(node)).strip()

    def stmt_of(self, node):
        while node in self.parent and not isinstance(node, ast.stmt):
            node = self.parent[node]
        return node


def _open_mode(expr):
    """'rb' for `open(p, "rb")` / `path.open("rb")`, 'r' for a plain open, None if not an open()."""
    if not isinstance(expr, ast.Call):
        return None
    f = expr.func
    if not ((isinstance(f, ast.Name) and f.id == 'open') or (isinstance(f, ast.Attribute) and f.attr == 'open')):
        return None
    args = list(expr.args[1:] if isinstance(f, ast.Name) else expr.args)
    mode = next((k.value for k in expr.keywords if k.arg == 'mode'), args[0] if args else None)
    return mode.value if isinstance(mode, ast.Constant) and isinstance(mode.value, str) else 'r'


def _tested(test):
    """Chains a condition tests for being set: `x`, `not x`, `x is None`, `x.y != None`,
    `isinstance(x, T)`, `len(x)`, joined by and/or. `x.name == "a"` tests nothing about x."""
    if isinstance(test, (ast.Name, ast.Attribute)):
        c = chain_text(test)
        return {c} if c else set()
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return _tested(test.operand)
    if isinstance(test, ast.BoolOp):
        return set().union(*[_tested(v) for v in test.values])
    if isinstance(test, ast.NamedExpr):
        return {test.target.id}
    if isinstance(test, ast.Compare):
        sides = [test.left] + test.comparators
        if any(isinstance(o, (ast.Is, ast.IsNot)) for o in test.ops) or \
                any(isinstance(x, ast.Constant) and x.value is None for x in sides):
            return {c for x in sides for c in [chain_text(x)] if c}
        return set()
    if isinstance(test, ast.Call) and isinstance(test.func, ast.Name) and test.args and \
            test.func.id in ('isinstance', 'hasattr', 'callable', 'bool', 'len', 'any', 'all'):
        c = chain_text(test.args[0])
        return {c} if c else set()
    return set()


def _stops(body):
    """A block that leaves: ends in raise/return/continue/break (at its top level)."""
    return any(isinstance(s, (ast.Raise, ast.Return, ast.Continue, ast.Break)) for s in body)


# ---------------------------------------------------------------- findings -----------------------

def fn_label(info):
    return (info['cls'] + '.' if info.get('cls') else '') + info['name']


def findings(info, idx, checker=None):
    """Type findings for one function, memoized on the index:
      calls: {call node: [finding]}  a finding drawn on that call's arrow
      rows:  [finding]               a line of this function the map doesn't show as a call row
    A finding is {got, want, at, tip, dim, line, where: 'call'|'ret', text (rows only), call}."""
    cache = idx.__dict__.setdefault('type_cache', {})
    if info['qual'] in cache:
        return cache[info['qual']]
    checker = checker or idx.__dict__.setdefault('type_checker', None) or Checker(idx)
    idx.type_checker = checker
    sc = Scope(info, idx)
    calls, rows, seen_rows, flagged = {}, [], set(), set()
    own_ret = info.get('returns') or ''
    # FastAPI turns a route's return value into its response model itself (an ORM row into
    # `UserPublic`, a dict into a schema), so there only a None is a mismatch
    is_route = info['qual'] in getattr(idx, 'route_quals', ())

    def producer(expr, line):
        """The call of ours that made the value `expr` holds (`c = repo.get(id)`), if any."""
        if isinstance(expr, ast.Name):
            r = sc.rec(expr.id, line)
            v = r[1] if r else None
        else:
            v = expr
        if isinstance(v, ast.Await):
            v = v.value
        if isinstance(v, ast.Call) and idx.resolve(v, info.get('cls'), info['qual']):
            return v
        return None

    def add_row(node, got, want, dim, tip, where_call=None):
        stmt = sc.stmt_of(node)
        at = sc.seg(node)
        key = (stmt.lineno, at)
        if key in seen_rows:
            return
        seen_rows.add(key)
        f = {'got': got, 'want': want, 'at': at, 'tip': tip, 'dim': dim, 'line': stmt.lineno,
             'text': _one_line(sc.seg(stmt)), 'where': 'ret', 'call': where_call}
        rows.append(f)
        if where_call is not None:   # the same finding, drawn on the arrow that brought the value
            calls.setdefault(where_call, []).append(f)

    def judge(got, want, bad, expr, line):
        """(got shown, want shown, uncertain) for a value that doesn't fit, or None when it does
        after all: its None was checked for before this line."""
        dim = got[1]
        g = got[0]
        if NONE in bad:
            c = none_check(expr, line)
            if c == 'yes':
                bad = [b for b in bad if b != NONE]
                g = without_none(g)
            dim = dim or c == 'maybe'
        if not bad:
            return None
        if bad == [NONE]:
            text = chain_text(expr)
            if text is not None:   # the same unchecked value handed on again: marked once is enough
                key = (text, sc.since(expr, line))
                if key in flagged:
                    return None
                flagged.add(key)
            gs, ws = none_only(g, want)
            return gs, ws, dim
        wm = [t for t in members(want) if t != NONE]
        # an ORM row for its schema, or a str for a str-based Enum: works often enough to say so quietly
        if all(checker.look_alike(b, t) or idx.is_sub(t, b) for b in bad if b != NONE for t in wm):
            dim = True
        return shown(g), pretty(want), dim

    def none_check(expr, line):
        """'no' / 'maybe' / 'yes': is a None in `expr` ruled out at `line`?"""
        text = chain_text(expr)
        if text is None:
            return 'no'
        return sc.checked(text, line, sc.since(expr, line))

    for n in sorted(_walk_own(info['node']), key=lambda x: (getattr(x, 'lineno', 0), getattr(x, 'col_offset', 0))):
        if not isinstance(n, ast.Call):
            continue
        q = idx.resolve(n, info.get('cls'), info['qual'])
        if not q:
            continue
        callee = idx.funcs[q]
        params = ann_params(callee['node'])
        pairs = []
        for i, a in enumerate(n.args):
            if isinstance(a, ast.Starred) or i >= len(params):
                break
            pairs.append((params[i], a))
        for k in n.keywords:
            p = next((p for p in params if p[0] == k.arg), None)
            if p:
                pairs.append((p, k.value))
        for (pname, ann, _), expr in pairs:
            if not ann:
                continue
            got = sc.type_of(expr, n.lineno)
            if not got:
                continue
            bad = checker.bad(got[0], ann)
            if not bad:
                continue
            r = judge(got, ann, bad, expr, n.lineno)
            if not r:
                continue
            g, w, dim = r
            dim = dim or not sc.sure(n)
            at = sc.seg(expr)
            calls.setdefault(n, []).append({
                'got': g, 'want': w, 'at': at, 'dim': dim, 'line': n.lineno,
                'where': 'call', 'tip': f"{fn_label(callee)}({pname}: {pretty(ann)})\n{at}: {shown(got[0])}"})

    # values handed back: `return repo.get(id)`, `c = repo.get(id)` then `c.name` / `return c`
    for n in sorted(_walk_own(info['node']), key=lambda x: (getattr(x, 'lineno', 0), getattr(x, 'col_offset', 0))):
        if isinstance(n, ast.Return) and n.value is not None and own_ret:
            got = sc.type_of(n.value, n.lineno)
            if not got:
                continue
            bad = checker.bad(got[0], own_ret)
            if is_route:
                bad = [b for b in bad if b == NONE]
            if not bad:
                continue
            r = judge(got, own_ret, bad, n.value, n.lineno)
            if not r:
                continue
            g, w, dim = r
            src = producer(n.value, n.lineno)
            tip = f"{fn_label(info)}(…) -> {pretty(own_ret)}\n{sc.seg(n.value)}: {shown(got[0])}"
            direct = src is not None and (n.value is src or getattr(n.value, 'value', None) is src)
            if direct:   # the call row itself is `return repo.get(id)`: mark it there
                calls.setdefault(src, []).append({'got': g, 'want': w, 'at': sc.seg(n.value), 'dim': dim,
                                                  'line': n.lineno, 'where': 'ret', 'tip': tip})
            else:
                add_row(n.value, g, w, dim, tip, src)
        elif (isinstance(n, (ast.Attribute, ast.Subscript)) and isinstance(n.value, ast.Name)
              and isinstance(n.ctx, ast.Load)):
            line = n.lineno
            got = sc.type_of(n.value, line)
            if not got or NONE not in members(got[0]) or members(got[0]) == [NONE]:
                continue
            c = none_check(n.value, line)
            since = sc.since(n.value, line)
            if c == 'yes' or (n.value.id, since) in flagged:   # one mark per value is enough
                continue
            flagged.add((n.value.id, since))
            g, w = shown(got[0]), without_none(shown(got[0]))
            if w == '…':
                g, w = NONE, ''
            tip = f"{sc.seg(n)}\n{n.value.id}: {shown(got[0])}"
            add_row(n, g, w, got[1] or c == 'maybe', tip, producer(n.value, line))
        elif isinstance(n, ast.FormattedValue) and n.conversion == -1:
            got = sc.type_of(n.value, n.lineno)
            if got and members(got[0]) == ['bytes']:
                add_row(n.value, 'bytes', 'str', got[1], f"f\"{{{sc.seg(n.value)}}}\" → \"b'…'\"",
                        producer(n.value, n.lineno))
        elif isinstance(n, ast.Compare) and len(n.ops) == 1 and isinstance(n.ops[0], (ast.Eq, ast.NotEq, ast.In, ast.NotIn)):
            a, b = sc.type_of(n.left, n.lineno), sc.type_of(n.comparators[0], n.lineno)
            if a and b:
                ta, tb = members(a[0]), members(b[0])
                for side, other, t in ((n.left, tb, ta), (n.comparators[0], ta, tb)):
                    if t == ['bytes'] and other == ['str']:
                        add_row(side, 'bytes', 'str', a[1] or b[1], f"{sc.seg(n)}\n{sc.seg(side)}: bytes",
                                producer(side, n.lineno))
                        break
        elif isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add):
            a, b = sc.type_of(n.left, n.lineno), sc.type_of(n.right, n.lineno)
            if a and b and {tuple(members(a[0])), tuple(members(b[0]))} == {('bytes',), ('str',)}:
                side = n.left if members(a[0]) == ['bytes'] else n.right
                add_row(side, 'bytes', 'str', a[1] or b[1], f"{sc.seg(n)}\n{sc.seg(side)}: bytes",
                        producer(side, n.lineno))
        elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'str'
              and len(n.args) == 1 and not n.keywords):
            got = sc.type_of(n.args[0], n.lineno)
            if got and members(got[0]) == ['bytes']:
                add_row(n.args[0], 'bytes', 'str', got[1], f"{sc.seg(n)} → \"b'…'\"",
                        producer(n.args[0], n.lineno))

    _merge_checker(info, idx, sc, calls, rows)
    out = {'calls': calls, 'rows': sorted(rows, key=lambda r: r['line'])}
    cache[info['qual']] = out
    return out


def _merge_checker(info, idx, sc, calls, rows):
    """Add what the repo's own mypy/pyright found in this function. Where flowsegul already marks
    that line, the checker agreeing makes an uncertain mark certain."""
    ext = getattr(idx, 'type_ext', {}).get(info['file'], {})
    fn = info['node']
    if not ext:
        return
    lines = (idx.file_src.get(info['file'], '')).splitlines()
    nodes = list(_walk_own(fn))
    for line, items in sorted(ext.items()):
        if not (fn.lineno < line <= fn.end_lineno) or line > len(lines):
            continue
        ours = [f for fl in calls.values() for f in fl if f['line'] == line] + [r for r in rows if r['line'] == line]
        for f in ours:
            f['dim'] = False
        if ours:
            continue
        stmts = [n for n in nodes if isinstance(n, ast.stmt) and n.lineno <= line <= n.end_lineno]
        if not stmts:
            continue
        stmt = min(stmts, key=lambda n: n.end_lineno - n.lineno)
        on_line = [n for n in nodes if isinstance(n, ast.Call) and n.lineno == line
                   and idx.resolve(n, info.get('cls'), info['qual'])]
        for e in items:
            if (info['qual'] in getattr(idx, 'route_quals', ()) and e['code'] in ('return-value', 'reportReturnType')
                    and not (NONE in members(e['got']) and NONE not in members(e['want']))):
                continue   # FastAPI converts a route's return value to its response model itself
            m = re.match(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*', lines[line - 1][e['col']:])
            at = m.group(0) if m else ''
            tip = f"{e['tool']} [{e['code']}]\n{lines[line - 1].strip()}"
            f = {'got': e['got'], 'want': e['want'], 'at': at, 'tip': tip, 'dim': False, 'line': line}
            arg = e['code'] in ('arg-type', 'reportArgumentType')
            ret_call = (isinstance(stmt, ast.Return) and stmt.value is not None
                        and e['code'] in ('return-value', 'reportReturnType'))
            direct = next((c for c in on_line if ret_call and (stmt.value is c or getattr(stmt.value, 'value', None) is c)), None)
            if arg and len(on_line) == 1:
                calls.setdefault(on_line[0], []).append(dict(f, where='call'))
            elif direct is not None:
                calls.setdefault(direct, []).append(dict(f, where='ret', at=sc.seg(stmt.value)))
            else:
                rows.append(dict(f, where='ret', call=None, text=_one_line(sc.seg(stmt))))


def none_only(got, want):
    """How a None-only finding reads on the map: 'Customer | None ≠ Customer' when the rest of the
    type fits as written, else just the None: 'None ≠ CustomerOut'."""
    rest = without_none(shown(got))
    return (shown(got), pretty(want)) if rest == pretty(want) else (NONE, pretty(want))


def shown(t):
    """Type text for the map: '? | None' (a query's .first()) reads as '… | None'."""
    return pretty(t).replace('?', '…') if t else t


def _one_line(s, limit=140):
    s = re.sub(r'\s+', ' ', (s or '').strip())
    return s[:limit] + ('…' if len(s) > limit else '')


# ---------------------------------------------------------------- mypy / pyright -----------------

_MYPY_CODES = {'arg-type', 'return-value', 'union-attr', 'assignment', 'str-bytes-safe', 'operator',
               'comparison-overlap'}
_PYRIGHT_RULES = {'reportArgumentType', 'reportReturnType', 'reportOptionalMemberAccess',
                  'reportAssignmentType', 'reportOptionalSubscript', 'reportOptionalIterable',
                  'reportOperatorIssue', 'reportGeneralTypeIssues'}


def _types_from_message(msg):
    """(got, want) out of a mypy/pyright message, or None if it isn't about a type that doesn't fit."""
    pats = [r'incompatible type "(.+?)"; expected "(.+?)"',                       # mypy arg-type
            r'\(got "(.+?)", expected "(.+?)"\)',                                  # mypy return-value
            r'\(expression has type "(.+?)", variable has type "(.+?)"\)',         # mypy assignment
            r'left operand type: "(.+?)", right operand type: "(.+?)"',            # comparison-overlap
            r'Unsupported operand types for \S+ \("(.+?)" and "(.+?)"\)',          # operator
            r'[Tt]ype "(.+?)" (?:cannot be assigned to|is not assignable to) (?:parameter "\w+" of type|return type|declared type|type) "(.+?)"']
    for p in pats:
        m = re.search(p, msg)
        if m:
            got, want = m.group(1), m.group(2)
            want = re.sub(r"^Literal\[['\"].*['\"]\]$", 'str', want)
            return got, want
    m = re.search(r'Item "None" of "(.+?)" has no attribute', msg)
    if m:
        return m.group(1), without_none(m.group(1))
    if re.search(r'is not a known attribute of "None"|Object of type "None" is not subscriptable', msg):
        return NONE, ''
    if 'b\'abc\'' in msg or "produces \"b'" in msg:
        return 'bytes', 'str'
    return None


def parse_type_output(text, repo):
    """{rel path: {line: [{got, want, col, tool, code}]}} from mypy text or pyright --outputjson."""
    out = {}

    def add(path, line, col, tool, code, msg):
        tw = _types_from_message(msg)
        if not tw:
            return
        rel = os.path.relpath(path, repo) if os.path.isabs(path) else os.path.normpath(path)
        out.setdefault(rel, {}).setdefault(line, []).append(
            {'got': pretty(tw[0]) if tw[0] != NONE else NONE, 'want': pretty(tw[1]) if tw[1] else '',
             'col': col, 'tool': tool, 'code': code})
    s = text.lstrip()
    if s.startswith('{'):
        try:
            data = json.loads(s)
        except ValueError:
            data = {}
        for d in data.get('generalDiagnostics', []):
            if d.get('severity') != 'error' or d.get('rule') not in _PYRIGHT_RULES:
                continue
            st = d.get('range', {}).get('start', {})
            add(d.get('file', ''), st.get('line', 0) + 1, st.get('character', 0), 'pyright', d.get('rule'),
                d.get('message', ''))
        return out
    for line in text.splitlines():
        m = re.match(r'^(.+?\.pyi?):(\d+):(?:(\d+):)?\s*error:\s*(.*?)\s*\[([\w-]+)\]\s*$', line)
        if m and m.group(5) in _MYPY_CODES:
            add(m.group(1), int(m.group(2)), max(0, int(m.group(3) or 1) - 1), 'mypy', m.group(5), m.group(4))
    return out


def _configured(repo):
    """Which checker the repo sets up: 'mypy', 'pyright' or None."""
    def read(name):
        try:
            with open(os.path.join(repo, name), encoding='utf-8', errors='replace') as f:
                return f.read()
        except OSError:
            return ''
    if os.path.exists(os.path.join(repo, 'pyrightconfig.json')) or '[tool.pyright]' in read('pyproject.toml'):
        return 'pyright'
    if (os.path.exists(os.path.join(repo, 'mypy.ini')) or os.path.exists(os.path.join(repo, '.mypy.ini'))
            or '[tool.mypy]' in read('pyproject.toml') or '[mypy' in read('setup.cfg')):
        return 'mypy'
    return None


def run_checker(repo, dirs, how='auto', log=print):
    """The repo's own type checker output, parsed ({} when there is none). `how`: 'auto' runs mypy
    or pyright when the repo configures it and it is installed, 'off' skips, else a saved output file."""
    if how == 'off':
        return {}
    if how != 'auto':
        try:
            with open(how, encoding='utf-8', errors='replace') as f:
                return parse_type_output(f.read(), repo)
        except OSError as e:
            log(f'  types: can\'t read {how} ({e.strerror})')
            return {}
    tool = _configured(repo)
    if not tool:
        return {}
    rels = [os.path.relpath(d, repo) for d in dirs if os.path.isdir(d)] or ['.']
    if tool == 'mypy':
        exe = shutil.which('mypy')
        cmd = ([exe] if exe else [sys.executable, '-m', 'mypy']) + [
            '--show-column-numbers', '--show-error-codes', '--no-error-summary', '--no-pretty',
            '--hide-error-context', '--no-color-output', *rels]
    else:
        exe = shutil.which('pyright')
        if not exe:
            log('  types: this repo sets up pyright, but it isn\'t installed; using flowsegul\'s own check')
            return {}
        cmd = [exe, '--outputjson', *rels]
    try:
        r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=240)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f'  types: {tool} didn\'t finish ({type(e).__name__}); using flowsegul\'s own check')
        return {}
    if tool == 'mypy' and 'No module named mypy' in r.stderr:
        log('  types: this repo sets up mypy, but it isn\'t installed; using flowsegul\'s own check')
        return {}
    if tool == 'mypy' and r.returncode == 2:   # mypy couldn't check the code at all
        first = (r.stdout.strip() or r.stderr.strip()).splitlines()[:1]
        log(f'  types: mypy stopped ({first[0] if first else "exit 2"}); using flowsegul\'s own check')
        return {}
    found = parse_type_output(r.stdout, repo)
    log(f'  types: read {tool} ({sum(len(v) for f in found.values() for v in f.values())} mismatches)')
    return found
