"""Review hints on single lines of a box's code, found by reading the code without running it.

  n1    an `await` of a repository call inside a loop body or a comprehension: one query per
        iteration (N+1). Certain.
  kw    a keyword a pydantic model / dataclass doesn't have as a field: pydantic drops it without a
        word (unless `extra="forbid"`), a dataclass raises. Certain when the class is the repo's own.
  miss  a defaulted model field the call leaves out, but only when the code itself suggests it was
        meant to be passed: another call to the same class passes it, or the call copies most of its
        values from one object whose type has an attribute of that name. Drawn as unsure.

Each hint is [line in the box's code (0-based), kind, tag, hover text, sure (1/0)].
"""
import ast, os, re

parse = ast.parse   # flowsegul_gen swaps in its own parser (reads newer syntax on older Pythons)
_DB_ROOTS = ('db_access',)
_PLAIN_BASES = {'object', 'Generic', 'ABC', 'Protocol'}


def _classes(idx):
    """{rel: {class name: ClassDef}} for the top-level classes of every indexed file."""
    if not hasattr(idx, '_hint_classes'):
        idx._hint_trees, idx._hint_classes = {}, {}
        for rel, src in idx.file_src.items():
            try:
                tree = parse(src)
            except SyntaxError:
                continue
            idx._hint_trees[rel] = tree
            idx._hint_classes[rel] = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    return idx._hint_classes


def _imported_from(idx, rel, name):
    """(module text, real name) a file imports `name` from, else None."""
    imp = idx.imports.get(rel, {}).get(name)
    return (imp[0], imp[1], imp[2]) if imp else None


def find_class(idx, rel, expr, _seen=None):
    """(rel, ClassDef) that a name or `module.Name` used in file `rel` refers to: a class in the same
    file, or one imported (following one re-export through a package `__init__`). None when unknown."""
    classes = _classes(idx)
    _seen = _seen or set()
    if (rel, expr) in _seen:
        return None
    _seen.add((rel, expr))
    if '.' in expr:                              # `models.ChatFile`: the module is imported
        head, name = expr.rsplit('.', 1)
        imp = _imported_from(idx, rel, head.split('.')[0])
        if not imp:
            return None
        mod = '.'.join([x for x in imp[:2] if x] + head.split('.')[1:])
        f = idx.module_file(rel, mod, imp[2])
        return find_class(idx, f, name, _seen) if f else None
    if expr in classes.get(rel, {}):
        return rel, classes[rel][expr]
    imp = _imported_from(idx, rel, expr)
    if not imp or not imp[1]:
        return None
    f = idx.module_file(rel, imp[0], imp[2])
    if not f:
        return None
    return find_class(idx, f, imp[1], _seen)


def _origin(idx, rel, name):
    """Where a name a file uses comes from: 'pydantic', 'dataclasses', … or '' when it's local."""
    imp = idx.imports.get(rel, {}).get(name.split('.')[0])
    return (imp[0] or '').split('.')[0] if imp else ''


def _is_dataclass_dec(idx, rel, dec):
    """(is a @dataclass, from pydantic, init=False) for a decorator."""
    fn = dec.func if isinstance(dec, ast.Call) else dec
    text = ast.unparse(fn)
    if text.split('.')[-1] != 'dataclass':
        return None
    pyd = 'pydantic' in text or _origin(idx, rel, text) == 'pydantic'
    no_init = isinstance(dec, ast.Call) and any(
        k.arg == 'init' and isinstance(k.value, ast.Constant) and k.value.value is False for k in dec.keywords)
    return pyd, no_init


def _field_default(value):
    """The default a field gets when left out, as source text; None when it has none (required)."""
    if value is None:
        return None
    if isinstance(value, ast.Call) and ast.unparse(value.func).split('.')[-1] in ('Field', 'field'):
        for k in value.keywords:
            if k.arg == 'default':
                return ast.unparse(k.value)
            if k.arg == 'default_factory':
                return ast.unparse(k.value) + '()'
        if value.args and not (isinstance(value.args[0], ast.Constant) and value.args[0].value is Ellipsis):
            return ast.unparse(value.args[0])
        return None
    return ast.unparse(value)


def _str_kw(call, name):
    v = next((k.value for k in call.keywords if k.arg == name), None)
    return v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else None


def model_info(idx, rel, cls, _seen=None):
    """What a call to a repo class accepts, or None when the code doesn't say:
    {kind: 'pydantic'|'dataclass', extra: 'ignore'|'forbid'|'allow', fields: {name: default text or
    None}, names: keywords accepted (fields + aliases), order: positional field order, open: True
    when some keyword could be accepted that we can't see (alias generator, before-validator)}."""
    _seen = _seen or set()
    key = (rel, cls.name)
    if key in _seen:
        return None
    _seen.add(key)
    kind, extra, opened = None, None, False
    for dec in cls.decorator_list:
        dc = _is_dataclass_dec(idx, rel, dec)
        if dc:
            if dc[1]:
                return None                      # @dataclass(init=False): its own __init__
            kind = 'dataclass'
            extra = 'ignore' if dc[0] else 'forbid'
    fields, names, order = {}, set(), []
    for b in cls.bases:                          # inherited fields and settings first
        text = ast.unparse(b)
        base = text.split('[')[0]
        last = base.split('.')[-1]
        if last == 'BaseModel' and (_origin(idx, rel, base) == 'pydantic' or base.startswith('pydantic')):
            kind = kind or 'pydantic'
            continue
        if last in _PLAIN_BASES:
            continue
        found = find_class(idx, rel, base)
        if not found:
            return None                          # a library base we can't read: fields unknown
        parent = model_info(idx, found[0], found[1], _seen)
        if not parent:
            return None
        kind = kind or parent['kind']
        extra = extra or parent['extra']
        opened = opened or parent['open']
        fields.update(parent['fields']); names |= parent['names']
        order += [f for f in parent['order'] if f not in order]
    if kind is None:
        return None
    if kind == 'pydantic' and any(k.arg == 'extra' for k in cls.keywords):
        extra = _str_kw(cls, 'extra') or extra
    for m in cls.body:
        if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if m.name == '__init__':
                return None                      # its own __init__ decides what it takes
            for d in m.decorator_list:
                dn = ast.unparse(d.func if isinstance(d, ast.Call) else d)
                if dn.endswith('model_validator') and isinstance(d, ast.Call) and \
                        _str_kw(d, 'mode') in ('before', 'wrap'):
                    opened = True                # may read keywords that aren't fields
                if dn.endswith('root_validator') and isinstance(d, ast.Call) and any(
                        k.arg == 'pre' and getattr(k.value, 'value', False) is True for k in d.keywords):
                    opened = True
        elif isinstance(m, (ast.Assign, ast.AnnAssign)) and m.value is not None and any(
                isinstance(t, ast.Name) and t.id == 'model_config'
                for t in (m.targets if isinstance(m, ast.Assign) else [m.target])):
            cfg = m.value
            if isinstance(cfg, ast.Call):
                extra = _str_kw(cfg, 'extra') or extra
                if any(k.arg == 'alias_generator' for k in cfg.keywords):
                    opened = True
            elif isinstance(cfg, ast.Dict):
                for k, v in zip(cfg.keys, cfg.values):
                    if isinstance(k, ast.Constant) and k.value == 'extra' and isinstance(v, ast.Constant):
                        extra = v.value
                    if isinstance(k, ast.Constant) and k.value == 'alias_generator':
                        opened = True
            else:
                opened = True                    # config built elsewhere: can't read it
        elif isinstance(m, ast.ClassDef) and m.name == 'Config':   # pydantic v1 style
            for c in m.body:
                if isinstance(c, ast.Assign) and any(getattr(t, 'id', '') == 'extra' for t in c.targets):
                    v = ast.unparse(c.value)
                    extra = v.split('.')[-1].strip('\'"')
                if isinstance(c, ast.Assign) and any(getattr(t, 'id', '') == 'alias_generator'
                                                     for t in c.targets):
                    opened = True
        elif isinstance(m, ast.AnnAssign) and isinstance(m.target, ast.Name):
            name, ann = m.target.id, ast.unparse(m.annotation)
            if re.match(r'(typing\.)?ClassVar\b', ann) or name.startswith('_') or ann == 'KW_ONLY' \
                    or ann.endswith('.KW_ONLY'):
                continue
            v = m.value
            if kind == 'dataclass' and isinstance(v, ast.Call) and \
                    ast.unparse(v.func).split('.')[-1] == 'field' and any(
                        k.arg == 'init' and getattr(k.value, 'value', True) is False for k in v.keywords):
                fields.pop(name, None)
                continue
            fields[name] = _field_default(v)
            if fields[name] and fields[name].startswith(ann + '.'):
                fields[name] = fields[name][len(ann) + 1:]   # ExtractionStatus.READY -> READY
            names.add(name)
            if name not in order:
                order.append(name)
            if isinstance(v, ast.Call) and ast.unparse(v.func).split('.')[-1] == 'Field':
                for a in ('alias', 'validation_alias'):
                    s = _str_kw(v, a)
                    if s:
                        names.add(s)
                    elif any(k.arg == a for k in v.keywords):
                        opened = True            # AliasChoices / AliasPath
    return {'kind': kind, 'extra': extra or 'ignore', 'fields': fields, 'names': names,
            'order': order, 'open': opened}


def class_attrs(idx, rel, cls, _seen=None):
    """Attribute names a class declares (annotations, `x = Column(...)`, `self.x = ...` in methods),
    its repo bases' included."""
    _seen = _seen or set()
    if (rel, cls.name) in _seen:
        return set()
    _seen.add((rel, cls.name))
    out = set()
    for m in cls.body:
        if isinstance(m, ast.AnnAssign) and isinstance(m.target, ast.Name):
            out.add(m.target.id)
        elif isinstance(m, ast.Assign):
            out |= {t.id for t in m.targets if isinstance(t, ast.Name)}
        elif isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for n in ast.walk(m):
                if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store) and \
                        isinstance(n.value, ast.Name) and n.value.id == 'self':
                    out.add(n.attr)
    for b in cls.bases:
        found = find_class(idx, rel, ast.unparse(b).split('[')[0])
        if found:
            out |= class_attrs(idx, found[0], found[1], _seen)
    return {a for a in out if not a.startswith('__')}


# ── where a call sits ──

def _own_nodes(node):
    """The nodes under `node` that run when it runs: not the bodies of nested defs or lambdas."""
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        for c in ast.iter_child_nodes(n):
            if not isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                stack.append(c)


def _fixed_literal(it):
    return isinstance(it, (ast.List, ast.Tuple, ast.Set)) and not any(
        isinstance(e, ast.Starred) for e in it.elts)


def _per_iteration(fn):
    """The parts of a function's loops and comprehensions that run once per item."""
    for n in ast.walk(fn):
        if isinstance(n, (ast.For, ast.AsyncFor)):
            if not _fixed_literal(n.iter):
                yield from n.body
        elif isinstance(n, ast.While):
            yield n.test
            yield from n.body
        elif isinstance(n, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            if _fixed_literal(n.generators[0].iter):
                continue
            yield from ([n.key, n.value] if isinstance(n, ast.DictComp) else [n.elt])
            for k, g in enumerate(n.generators):
                yield from g.ifs
                if k:
                    yield g.iter


def _is_query(call, info, idx):
    chain = ast.unparse(call.func).split('.')
    if any(c in _DB_ROOTS for c in chain[:-1]):
        return True
    q = idx.resolve(call, info['cls'], info['qual'])
    return bool(q) and idx.layer(q) == 'repository'


def query_in_loop(info, idx, cl):
    out, seen = [], set()
    for part in _per_iteration(info['node']):
        for n in _own_nodes(part):
            if isinstance(n, ast.Await) and isinstance(n.value, ast.Call) and n.lineno not in seen \
                    and _is_query(n.value, info, idx):
                seen.add(n.lineno)
                out.append([cl(n.lineno), 'n1', 'query per iteration',
                            'query per iteration (N+1)', 1])
    return out


# ── what type a name holds ──

def _elem_class(idx, rel, ann):
    """The repo class an annotation is about, looking inside list[…] / Optional[…] / Sequence[…]."""
    for name in re.findall(r'[A-Za-z_][A-Za-z0-9_.]*', ann or ''):
        found = find_class(idx, rel, name)
        if found:
            return found
    return None


def _returns_of(call, info, idx):
    """(file, return annotation) of the repo function a call goes to."""
    q = idx.resolve(call, info['cls'], info['qual'])
    if not q:
        return None
    f = idx.funcs[q]
    return f['file'], f['returns']


def _value_class(value, info, idx):
    v = value.value if isinstance(value, ast.Await) else value
    if isinstance(v, ast.Call):
        found = find_class(idx, info['file'], ast.unparse(v.func))
        if found:
            return found
        r = _returns_of(v, info, idx)
        return _elem_class(idx, r[0], r[1]) if r else None
    if isinstance(v, ast.Name):
        return name_class(v.id, info, idx)
    return None


def name_class(name, info, idx):
    """(rel, ClassDef) of what local `name` holds: its annotation, the loop it's the target of (the
    element type of what's iterated), or a single assignment. None when the code doesn't say."""
    fn = info['node']
    a = fn.args
    for arg in [*getattr(a, 'posonlyargs', []), *a.args, *a.kwonlyargs]:
        if arg.arg == name and arg.annotation is not None:
            return _elem_class(idx, info['file'], ast.unparse(arg.annotation))
    srcs = []
    for n in ast.walk(fn):
        if isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension)) and \
                isinstance(n.target, ast.Name) and n.target.id == name:
            srcs.append(n.iter)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == name:
            return _elem_class(idx, info['file'], ast.unparse(n.annotation))
        elif isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in n.targets):
            srcs.append(n.value)
    got = [_value_class(s, info, idx) for s in srcs]
    if not got or not all(got) or len({(r, c.name) for r, c in got}) != 1:
        return None
    return got[0]


# ── constructor calls ──

def _where(idx, rel, fn_node, cls_name):
    stem = os.path.basename(rel)[:-3]
    return f'{cls_name}.{fn_node.name}' if cls_name else f'{stem}.{fn_node.name}'


def _ctor_calls(idx):
    """Every call in the repo to a model class: [(class key, call, rel, enclosing fn, its class)]."""
    if hasattr(idx, '_hint_ctor'):
        return idx._hint_ctor
    out = []
    _classes(idx)
    for rel, tree in idx._hint_trees.items():
        def visit(node, fn, cname):
            for c in ast.iter_child_nodes(node):
                if isinstance(c, ast.ClassDef):
                    visit(c, fn, c.name if fn is None else cname)
                elif isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    visit(c, c if fn is None else fn, cname)
                else:
                    if isinstance(c, ast.Call) and isinstance(c.func, (ast.Name, ast.Attribute)):
                        found = find_class(idx, rel, ast.unparse(c.func))
                        if found:
                            out.append(((found[0], found[1].name), c, rel, fn, cname))
                    visit(c, fn, cname)
        visit(tree, None, None)
    idx._hint_ctor = out
    return out


def _passed(call, mi):
    """Field names a call gives a value, or None when it can't be told (*args / **kwargs)."""
    if any(isinstance(a, ast.Starred) for a in call.args) or any(k.arg is None for k in call.keywords):
        return None
    got = {k.arg for k in call.keywords}
    if mi['kind'] == 'dataclass':
        got |= set(mi['order'][:len(call.args)])
    return got


def model_calls(info, idx, cl):
    out = []
    rel, fn = info['file'], info['node']
    for call in ast.walk(fn):
        if not (isinstance(call, ast.Call) and isinstance(call.func, (ast.Name, ast.Attribute))):
            continue
        found = find_class(idx, rel, ast.unparse(call.func))
        if not found:
            continue
        mi = model_info(idx, *found)
        if not mi:
            continue
        cname = found[1].name
        got = _passed(call, mi)
        if got is None:
            continue
        # hint 2: a keyword the class has no field for
        if not mi['open'] and mi['extra'] != 'allow':
            for k in call.keywords:
                if k.arg in mi['names']:
                    continue
                raises = mi['kind'] == 'dataclass' or mi['extra'] == 'forbid'
                out.append([cl(k.lineno), 'kw', 'not a field, ' + ('raises' if raises else 'dropped'),
                            f'`{k.arg}` is not a field of {cname}, '
                            + ('raises at runtime' if raises else 'silently dropped'), 1])
        # hint 3: a defaulted field left out that the code suggests was meant to be passed
        missing = [f for f, d in mi['fields'].items() if d is not None and f not in got]
        if not missing:
            continue
        # (a) counts calls that build the same shape (every field this call passes), and only when
        # at least as many of them pass the field as leave it out
        own = got & set(mi['fields'])
        if len(own) < 2:
            own = None   # one field or none says nothing about which shape this call builds
        others, skip = {}, {}
        for key, c2, rel2, fn2, cls2 in (_ctor_calls(idx) if own else []):
            if key != (found[0], cname) or fn2 is None or \
                    (rel2, c2.lineno, c2.col_offset) == (rel, call.lineno, call.col_offset):
                continue
            g2 = _passed(c2, mi) or set()
            if not own <= g2:
                continue
            for f in missing:
                skip.setdefault(f, 0)
                skip[f] += f not in g2
                if f in g2:
                    others.setdefault(f, []).append(_where(idx, rel2, fn2, cls2))
        objs = {}
        for k in call.keywords:
            v = k.value
            if isinstance(v, ast.Attribute) and isinstance(v.value, ast.Name):
                objs[v.value.id] = objs.get(v.value.id, 0) + 1
        src, attrs = None, set()
        if objs:
            name, n = max(objs.items(), key=lambda x: x[1])
            if n >= 2 and n * 2 > len(call.keywords):
                tc = name_class(name, info, idx)
                if tc:
                    src, attrs = name, class_attrs(idx, *tc)
        found_here = []
        for f in missing:
            has = src and f in attrs
            who = list(dict.fromkeys(others.get(f, [])))
            if len(others.get(f, [])) < skip.get(f, 0):
                who = []   # more calls of the same shape leave it out: it is optional here
            if not has and not who:
                continue
            found_here.append((f, has, who))
        if len(found_here) > 2:
            continue   # leaving out several such fields is a deliberate subset, not a slip
        for f, has, who in found_here:
            said = f'not passed (defaults to {mi["fields"][f]})'
            passes = (who[0] + (f' and {len(who) - 1} more' if len(who) > 1 else '')
                      + (' pass it' if len(who) > 1 else ' passes it')) if who else ''
            if has:
                tip = f'{src} has {f}; {said}' + (f'; {passes}' if passes else '')
            else:
                tip = f'{f} {said}; {passes}'
            out.append([cl(call.func.lineno), 'miss', f'{f} not passed?', tip, 0])
    return out


def hints_for(info, idx, cl):
    key = info['qual']
    cache = idx.__dict__.setdefault('_hint_cache', {})
    if key not in cache:
        try:
            cache[key] = query_in_loop(info, idx, cl) + model_calls(info, idx, cl)
        except RecursionError:
            cache[key] = []
    return cache[key]
