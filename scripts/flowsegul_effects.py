"""What a route does to the world, in the order it happens: the side-effect strip.

Walks a route's code the way a request runs it: its dependencies up to their `yield`, the body
(following calls into the repo's own functions), then the dependencies' code after `yield`, then
background tasks. On the way it records

  write    session.add / delete / merge, execute(insert|update|delete …), query(…).delete()
  commit   session.commit(), the end of `with session.begin():`, or a commit after `yield` in a
           Depends generator (then `via` names that dependency, e.g. get_db)
  out      a call that can't be taken back: email, HTTP POST/PUT/PATCH/DELETE, stripe, boto3
           writes, files, queues; plus anything `.flowsegul.json` lists under "irreversible"
  later    background_tasks.add_task(fn): runs after the response
  fail     a `raise` nothing on the way catches (not shown; used for the red marks)

An `out` step gets `risk` (the errors that can still happen after it, and `commit` when a commit
still follows) only when the route commits somewhere we can see: then an error or a failed commit
leaves the database rolled back while the email is already sent. With writes but no commit found,
the strip ends in `commit ?` and nothing is marked.
"""
import ast, fnmatch, re

_SESSION_NAME = re.compile(r'(^|_)(session|sess|db|conn|connection|uow|tx|transaction)$', re.I)
_WRITE_METHODS = {'add': 'add', 'add_all': 'add', 'delete': 'delete', 'merge': 'merge',
                  'bulk_save_objects': 'add', 'bulk_insert_mappings': 'insert',
                  'bulk_update_mappings': 'update', 'insert_one': 'insert', 'insert_many': 'insert',
                  'update_one': 'update', 'update_many': 'update', 'replace_one': 'update',
                  'delete_one': 'delete', 'delete_many': 'delete'}
_SQL_WRITE = re.compile(r'\b(insert\s+into|update|delete\s+from|merge\s+into|upsert\s+into)\s+["`\[]?(\w+)', re.I)
_HTTP_LIBS = ('httpx', 'requests', 'aiohttp', 'urllib3')
_HTTP_VERBS = {'post', 'put', 'patch', 'delete'}
# (glob on the call's full dotted name, chip label). First match wins.
_OUTSIDE = [
    ('smtplib.*', 'email'), ('aiosmtplib.*', 'email'), ('fastapi_mail.*.send_message', 'email'),
    ('emails.*.send', 'email'), ('sendgrid.*.send', 'email'), ('resend.*.send', 'email'),
    ('postmarker.*.send*', 'email'), ('yagmail.*.send', 'email'), ('mailchimp_transactional.*.send*', 'email'),
    ('django.core.mail.send*', 'email'), ('boto3.client.send_email', 'email'),
    ('boto3.client.send_raw_email', 'email'), ('mailjet_rest.*.create', 'email'),
    ('stripe.*.create', 'stripe'), ('stripe.*.modify', 'stripe'), ('stripe.*.delete', 'stripe'),
    ('stripe.*.cancel', 'stripe'), ('stripe.*.capture', 'stripe'), ('stripe.*.confirm', 'stripe'),
    ('stripe.*.pay', 'stripe'), ('stripe.*.refund', 'stripe'), ('stripe.*.void_invoice', 'stripe'),
    ('stripe.*.finalize_invoice', 'stripe'), ('stripe.*.send_invoice', 'stripe'),
    ('twilio.*.create', 'sms'), ('slack_sdk.*.chat_postMessage', 'slack'), ('slack.*.chat_postMessage', 'slack'),
    ('redis.*.publish', 'publish'), ('pika.*.basic_publish', 'publish'), ('aio_pika.*.publish', 'publish'),
    ('google.cloud.pubsub*.publish', 'publish'), ('kafka.*.send', 'publish'), ('aiokafka.*.send*', 'publish'),
    ('confluent_kafka.*.produce', 'publish'), ('nats.*.publish', 'publish'),
    ('os.remove', 'delete file'), ('os.unlink', 'delete file'), ('os.rmdir', 'delete file'),
    ('shutil.rmtree', 'delete file'), ('os.rename', 'move file'), ('os.replace', 'move file'),
    ('shutil.move', 'move file'), ('shutil.copy*', 'write file'),
]
_BOTO_WRITE = re.compile(r'^(put_|delete_|send_|publish|upload_|create_|update_|invoke|start_|copy_|'
                         r'remove_|write_|batch_write|terminate_|stop_|run_instances)')
_NOT_CLASS = {'Optional', 'Union', 'List', 'list', 'Dict', 'dict', 'Annotated', 'Sequence', 'Iterable',
              'Any', 'None', 'Type', 'type', 'Set', 'set', 'Tuple', 'tuple'}
_MAX_VISITS = 400


def _post_order(node):
    """Calls (and yields) in an expression in the order Python runs them: arguments first."""
    out = []

    def visit(n):
        if isinstance(n, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return
        for ch in ast.iter_child_nodes(n):
            visit(ch)
        if isinstance(n, (ast.Call, ast.Yield, ast.YieldFrom)):
            out.append(n)
    visit(node)
    return out


def _has_yield(fn):
    stack = list(fn.body)
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.Yield, ast.YieldFrom)):
            return True
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(n))
    return False


def _class_in(text):
    for t in re.findall(r'[A-Za-z_][A-Za-z0-9_]*', text or ''):
        if t[0].isupper() and t not in _NOT_CLASS:
            return t
    return None


class _Walk:
    def __init__(self, idx, G, nid, cfg):
        self.idx, self.G, self.nid = idx, G, nid
        self.irrev = [p for p in (cfg.get('irreversible') or []) if isinstance(p, str)]
        self.head, self.tail, self.later = [], [], []
        self.sink = self.head
        self.loop_n = 0
        self.visits = 0
        self.no_fail = False     # a dependency's code after `yield` runs only once the route is done
        self.dep_via = None      # name of the Depends generator being walked

    # ── helpers ──
    def frame(self, qual):
        cache = self.idx.__dict__.setdefault('fx_frames', {})
        if qual not in cache:
            cache[qual] = self._frame(qual)
        return cache[qual]

    def _frame(self, qual):
        info = self.idx.funcs[qual]
        types = dict(self.idx.module_types.get(info['file'], {}))
        types.update(self.idx._types_in(qual))
        for n in ast.walk(info['node']):       # `with httpx.Client() as client:`
            if isinstance(n, (ast.With, ast.AsyncWith)):
                for it in n.items:
                    if isinstance(it.optional_vars, ast.Name):
                        t = self.G._ctor_type(it.context_expr)
                        if t:
                            types[it.optional_vars.id] = t
        return {'qual': qual, 'info': info, 'rel': info['file'], 'cls': info.get('cls'), 'types': types,
                'title': (f"{info['cls']}." if info['cls'] else '') + info['name'],
                'src': self.idx.file_src.get(info['file'], '')}

    def imported(self, name, rel):
        imp = self.idx.imports.get(rel, {}).get(name)
        if not imp:
            return None
        mod, attr, level = imp
        if level:
            return None
        return '.'.join(x for x in (mod, attr) if x)

    def type_path(self, text, rel):
        """Full dotted name of the class a type text names, through `rel`'s imports:
        'Annotated[httpx.AsyncClient, Depends(get_client)]' → 'httpx.AsyncClient'."""
        for w in re.findall(r'[A-Za-z_][\w.]*', re.sub(r'(["\']).*?\1', '', text or '')):
            if w in _NOT_CLASS or w in ('Depends', 'Security') or ('.' not in w and not w[0].isupper()):
                continue
            first, _, rest = w.partition('.')
            p = self.imported(first, rel)
            return (p + ('.' + rest if rest else '')) if p else w
        return None

    def returned_type(self, func_expr, F):
        """What an in-repo function (a Depends provider, a factory) is annotated to return."""
        q = self.idx.resolve(ast.Call(func=func_expr, args=[], keywords=[]), F['cls'], F['qual'])
        info = self.idx.funcs.get(q)
        return self.type_path(info['returns'], info['file']) if info and info['returns'] else None

    def name_type(self, name, F):
        """Full dotted class of a variable: its annotation or `x = Cls()`, a Depends provider's
        return type, or a module-level object imported from another file (`from app.clients import http`)."""
        t = F['types'].get(name)
        if t:
            if '.' not in t and not t[:1].isupper() and not t.startswith(('Annotated', 'Optional')):
                return self.returned_type(ast.Name(id=t), F)      # x = get_client()
            return self.type_path(t, F['rel'])
        for pname, dep in self.G.dependencies(F['info'], self.idx):
            if pname == name:
                return self.returned_type(dep.args[0], F)
        imp = self.idx.imports.get(F['rel'], {}).get(name)
        if imp and imp[1]:
            f = self.idx.module_file(F['rel'], imp[0], imp[2])
            t = self.idx.module_types.get(f, {}).get(imp[1]) if f else None
            if t:
                return self.type_path(t, f)
        return None

    def attr_type(self, attr, F):
        """`self.client` with `self.client = httpx.AsyncClient()` in the class (or a base)."""
        cls = F['cls']
        for c in [cls] + self.idx.bases.get(cls, []) if cls else []:
            for rel, types in self.idx.attr_types.get(c, []):
                if attr in types:
                    return self.type_path(types[attr], rel)
        return None

    def dotted(self, e, F, depth=0):
        """Full dotted name of a callee, through imports and known types:
        `client.post` with `client = httpx.AsyncClient()` → 'httpx.AsyncClient.post'."""
        parts = []
        while isinstance(e, ast.Attribute):
            parts.append(e.attr)
            e = e.value
        if isinstance(e, ast.Call) and depth < 4:
            base = self.dotted(e.func, F, depth + 1)
        elif isinstance(e, ast.Name) and e.id == 'self' and parts:
            base = self.attr_type(parts[-1], F)
            if base:
                parts.pop()
            else:
                base = 'self'
        elif isinstance(e, ast.Name):
            base = (self.name_type(e.id, F) if depth < 4 else None) or self.imported(e.id, F['rel']) or e.id
        else:
            return None
        return '.'.join([base] + parts[::-1]) if base else None

    def sessionish(self, recv, F):
        last = recv.attr if isinstance(recv, ast.Attribute) else getattr(recv, 'id', '')
        d = self.dotted(recv, F) or ''
        if d.split('.')[0] in _HTTP_LIBS:
            return False
        if last and _SESSION_NAME.search(last):
            return True
        return bool(re.search(r'(Session|Connection)\b', d)) and 'Client' not in d

    def obj_label(self, a, F):
        if isinstance(a, ast.Starred):
            a = a.value
        if isinstance(a, ast.Call):
            return _class_in(self.G.unparse(a.func)) or self.G.unparse(a.func).split('.')[-1]
        if isinstance(a, ast.Name):
            return self.unalias(_class_in(F['types'].get(a.id)), F) or self.fetched(a.id, F) or a.id
        return self.G.one_line(self.G.unparse(a), 24)

    def unalias(self, name, F):
        """`CurrentUser = Annotated[User, Depends(…)]` → 'User'."""
        if not name or name in self.idx.bases:
            return name
        rel, key = F['rel'], name
        imp = self.idx.imports.get(rel, {}).get(name)
        if imp and imp[1]:
            rel, key = self.idx.module_file(rel, imp[0], imp[2]) or rel, imp[1]
        v = self.idx.module_values.get(rel, {}).get(key)
        if isinstance(v, ast.Subscript) and self.G.unparse(v.value).split('.')[-1] == 'Annotated':
            first = v.slice.elts[0] if isinstance(v.slice, ast.Tuple) else v.slice
            return _class_in(self.G.unparse(first)) or name
        return name

    def fetched(self, name, F):
        """`doc = session.get(Document, id)` / `db.query(User)….first()` / `select(User)`: the model."""
        v = self.stmt_value(ast.Name(id=name), F)
        for n in ast.walk(v) if v is not None else ():
            if (isinstance(n, ast.Call) and n.args and isinstance(n.args[0], ast.Name) and n.args[0].id[:1].isupper()
                    and self.G.unparse(n.func).split('.')[-1] in ('get', 'get_one', 'query', 'select', 'get_or_404')):
                return n.args[0].id
        return None

    def stmt_value(self, e, F):
        """`session.execute(stmt)`: the statement `stmt` was built from, if assigned in this function."""
        if isinstance(e, ast.Name):
            for n in ast.walk(F['info']['node']):
                if (isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
                        and n.targets[0].id == e.id):
                    return n.value
        return e

    def sql_write(self, e, F):
        e = self.stmt_value(e, F)
        for n in ast.walk(e):
            if isinstance(n, ast.Call):
                fname = self.G.unparse(n.func)
                verb = fname.split('.')[-1]
                if verb in ('insert', 'update', 'delete'):
                    if n.args:                        # insert(Order) / update(User)
                        return f'{verb} {self.obj_label(n.args[0], F)}'
                    if isinstance(n.func, ast.Attribute):   # orders_table.insert()
                        return f'{verb} {self.G.unparse(n.func.value).split(".")[-1]}'
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                m = _SQL_WRITE.search(n.value)
                if m:
                    return f'{m.group(1).split()[0].lower()} {m.group(2)}'
            if isinstance(n, ast.JoinedStr):
                txt = ''.join(v.value for v in n.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
                m = _SQL_WRITE.search(txt)
                if m:
                    return f'{m.group(1).split()[0].lower()} {m.group(2)}'
        return None

    def config_hit(self, names):
        return any(fnmatch.fnmatch(n, p) for p in self.irrev for n in names if n)

    # ── what one call is ──
    def classify(self, call, F):
        """(kind, label) for a call that is itself an effect, else None."""
        fn = call.func
        G = self.G
        dotted = self.dotted(fn, F) or ''
        text = G.unparse(fn)
        if self.irrev and self.config_hit([dotted, text]):
            return 'out', text.split('.')[-1] if len(text) > 28 else text
        if isinstance(fn, ast.Attribute):
            m, recv = fn.attr, fn.value
            if m == 'commit' and not call.args and self.sessionish(recv, F):
                return 'commit', 'commit'
            if m in _WRITE_METHODS and self.sessionish(recv, F) and (call.args or call.keywords):
                arg = call.args[0] if call.args else call.keywords[0].value
                return 'write', f'{_WRITE_METHODS[m]} {self.obj_label(arg, F)}'
            if m in ('execute', 'exec', 'executemany') and call.args and self.sessionish(recv, F):
                w = self.sql_write(call.args[0], F)
                return ('write', w) if w else None
            if m in ('delete', 'update') and 'query(' in G.unparse(recv):   # session.query(User)….delete()
                q = next((c for c in ast.walk(recv) if isinstance(c, ast.Call)
                          and getattr(c.func, 'attr', '') == 'query' and c.args), None)
                return 'write', f'{m} {self.obj_label(q.args[0], F) if q else ""}'.strip()
            if m in ('delay', 'apply_async') or (m == 'send_task' and call.args):
                name = (G.unparse(call.args[0]).strip('\'"') if m == 'send_task'
                        else G.unparse(recv).split('.')[-1])
                return 'out', f'queue {name}'
            if m in ('write_text', 'write_bytes'):
                return 'out', 'write file'
            if m == 'unlink' and not call.args:
                return 'out', 'delete file'
            if m == 'add_task' and call.args:
                return 'later', f'add_task {G.unparse(call.args[0]).split(".")[-1]}'
        root = dotted.split('.')[0]
        last = dotted.split('.')[-1]
        if root in _HTTP_LIBS:
            verb = last.lower()
            if verb == 'request' and call.args and isinstance(call.args[0], ast.Constant):
                verb = str(call.args[0].value).lower()
            if verb in _HTTP_VERBS:
                url = call.args[1] if last.lower() == 'request' and len(call.args) > 1 else (
                    call.args[0] if call.args and last.lower() != 'request' else None)
                host = re.match(r'https?://([^/\s{]+)', url.value) if isinstance(url, ast.Constant) \
                    and isinstance(url.value, str) else None
                return 'out', verb.upper() + (' ' + host.group(1) if host else '')
            return None
        if dotted.startswith('boto3.') and _BOTO_WRITE.match(last):
            for pat, label in _OUTSIDE:
                if fnmatch.fnmatch(dotted, pat):
                    return 'out', label
            return 'out', f'boto3 {last}'
        for pat, label in _OUTSIDE:
            if fnmatch.fnmatch(dotted, pat):
                if label == 'email' and root in ('smtplib', 'aiosmtplib') and not last.startswith('send'):
                    continue            # SMTP(...), login(), starttls(): not the send itself
                return 'out', label
        if text in ('open', 'aiofiles.open', 'io.open') or dotted == 'aiofiles.open':
            mode = call.args[1] if len(call.args) > 1 else next(
                (k.value for k in call.keywords if k.arg == 'mode'), None)
            if isinstance(mode, ast.Constant) and isinstance(mode.value, str) and re.search(r'[wax+]', mode.value):
                return 'out', 'write file'
        return None

    def segment(self, F, node):
        """The call's own source text (ast.get_source_segment re-splits the file every time)."""
        cache = self.idx.__dict__.setdefault('fx_lines', {})
        lines = cache.get(F['rel'])
        if lines is None:
            lines = cache[F['rel']] = F['src'].splitlines()
        a, b = getattr(node, 'lineno', 0), getattr(node, 'end_lineno', None) or getattr(node, 'lineno', 0)
        if not a or b > len(lines):
            return None
        part = lines[a - 1:b]
        part[-1] = part[-1][:node.end_col_offset] if len(part) > 1 else part[-1][node.col_offset:node.end_col_offset]
        if len(part) > 1:
            part[0] = part[0][node.col_offset:]
        return '\n'.join(part)

    def lookup(self, c, F):
        """(effect, in-repo targets) of one call site, worked out once for all routes."""
        cache = self.idx.__dict__.setdefault('fx_calls', {})
        key = (id(c), F['qual'])
        if key not in cache:
            eff = self.classify(c, F)
            targets = []
            if not eff:
                q = self.idx.resolve(c, F['cls'], F['qual'])
                targets = [q] if q in self.idx.funcs else []
                if not targets:   # a function handed over by name: asyncio.to_thread(fn, …)
                    for a in list(c.args) + [k.value for k in c.keywords]:
                        if isinstance(a, ast.Name) and a.id not in F['types']:
                            r = self.idx.resolve(ast.Call(func=a, args=[], keywords=[]), F['cls'], F['qual'])
                            if r in self.idx.funcs and r != F['qual']:
                                targets.append(r)
            cache[key] = (eff, targets, c)     # keep c alive so its id stays unique
        return cache[key][:2]

    # ── walking ──
    def emit(self, kind, label, F, node, ctx, **extra):
        seg = self.segment(F, node)
        ev = {'k': kind, 'label': label, 'cond': ctx['cond'], 'loops': ctx['loops'],
              'fn': F['title'], 'file': F['rel'], 'line': getattr(node, 'lineno', 0),
              'code': self.G.one_line(seg or self.G.unparse(node), 90), 'node': self.nid(F['qual'])}
        if getattr(node, 'lineno', None):     # the line in that function's box, to mark it there
            cl = self.G.code_lines_of(F['info'], self.idx.file_src.get(F['rel'], ''))
            ev['ln'] = [cl(node.lineno), cl(getattr(node, 'end_lineno', None) or node.lineno)]
        ev.update(extra)
        if kind == 'later':
            self.later.append(ev)
        else:
            self.sink.append(ev)

    def via(self, kind):
        """A commit after a dependency's `yield` isn't written on the route's path: say where it is."""
        return {'via': self.dep_via} if kind == 'commit' and self.dep_via and self.sink is self.tail else {}

    def fail(self, t, ctx):
        if not self.no_fail:
            self.sink.append({'k': 'fail', 't': t, 'loops': ctx['loops']})

    def exprs(self, nodes, F, ctx, depth, stack):
        for en in nodes:
            if en is None:
                continue
            for c in _post_order(en):
                if isinstance(c, (ast.Yield, ast.YieldFrom)):
                    if self.dep_via:          # the route runs here; what follows runs after it
                        self.sink, self.no_fail = self.tail, True
                    continue
                self.call(c, F, ctx, depth, stack)

    def call(self, c, F, ctx, depth, stack):
        idx, G = self.idx, self.G
        eff, targets = self.lookup(c, F)
        if eff:
            kind, label = eff
            self.emit(kind, label, F, c, ctx, **self.via(kind))
            return
        for t in targets:
            info = idx.funcs[t]
            if self.irrev and self.config_hit([info['name'], f"{info['cls']}.{info['name']}" if info['cls'] else '',
                                               info['file'][:-3].replace('/', '.') + '.' + info['name']]):
                self.emit('out', info['name'], F, c, ctx)
                continue
            if t in stack or depth >= 10 or self.visits >= _MAX_VISITS:
                for e in G.escapes(t, idx):
                    if not G.caught_by(e[0], ctx['catch'], idx):
                        self.fail(e[0], ctx)
                continue
            self.func(t, ctx, depth + 1, stack)

    def func(self, qual, ctx, depth, stack):
        self.visits += 1
        F = self.frame(qual)
        self.stmts(F['info']['node'].body, F, dict(ctx, handling=None), depth, stack | {qual})

    def stmts(self, body, F, ctx, depth, stack):
        G = self.G
        for s in body:
            if isinstance(s, ast.If):
                self.exprs([s.test], F, ctx, depth, stack)
                test = G.one_line(G.unparse(s.test), 28)
                self.stmts(s.body, F, dict(ctx, cond='if ' + test), depth, stack)
                self.stmts(s.orelse, F, dict(ctx, cond='if not ' + test), depth, stack)
            elif isinstance(s, (ast.For, ast.AsyncFor, ast.While)):
                self.exprs([getattr(s, 'iter', None) or s.test], F, ctx, depth, stack)
                self.loop_n += 1
                head = (f'for {G.unparse(s.target)} in {G.unparse(s.iter)}' if hasattr(s, 'iter')
                        else f'while {G.unparse(s.test)}')
                inner = dict(ctx, cond=G.one_line(head, 28), loops=ctx['loops'] + (self.loop_n,))
                self.stmts(s.body, F, inner, depth, stack)
                self.stmts(s.orelse, F, dict(ctx, cond=ctx['cond'] or G.one_line(head, 28)), depth, stack)
            elif isinstance(s, (ast.With, ast.AsyncWith)):
                self.exprs([it.context_expr for it in s.items], F, ctx, depth, stack)
                begin = next((it.context_expr for it in s.items
                              if isinstance(it.context_expr, ast.Call)
                              and getattr(it.context_expr.func, 'attr', '') == 'begin'
                              and self.sessionish(it.context_expr.func.value, F)), None)
                self.stmts(s.body, F, ctx, depth, stack)
                if begin is not None:
                    self.emit('commit', 'commit', F, begin, ctx, **self.via('commit'))
            elif isinstance(s, ast.Try) or type(s).__name__ == 'TryStar':
                handlers = [G.handler_info(h) for h in s.handlers]
                self.stmts(s.body, F, dict(ctx, catch=ctx['catch'] + handlers), depth, stack)
                for h, hi in zip(s.handlers, handlers):
                    self.stmts(h.body, F, dict(ctx, cond='except ' + ', '.join(hi[0]).replace('*', ''),
                                               handling=hi[0][0]), depth, stack)
                self.stmts(s.orelse, F, ctx, depth, stack)
                self.stmts(s.finalbody, F, ctx, depth, stack)
            elif type(s).__name__ == 'Match':
                self.exprs([s.subject], F, ctx, depth, stack)
                for case in s.cases:
                    self.stmts(case.body, F, dict(ctx, cond='case ' + G.one_line(G.unparse(case.pattern), 22)),
                               depth, stack)
            elif isinstance(s, ast.Raise):
                self.exprs([s.exc, s.cause], F, ctx, depth, stack)
                t = G.exc_name(s.exc) if s.exc is not None else ctx.get('handling')
                if t and t != '*' and not G.caught_by(t, ctx['catch'], self.idx):
                    self.fail(t, ctx)
            elif isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            else:
                self.exprs([n for n in ast.iter_child_nodes(s) if isinstance(n, ast.expr)], F, ctx, depth, stack)

    def deps(self, qual, seen, ctx):
        """Run a function's FastAPI dependencies (their own dependencies first), up to `yield`;
        the part after `yield` goes to the tail, which runs after the route body."""
        G, idx = self.G, self.idx
        info = idx.funcs[qual]
        for _p, dep in G.dependencies(info, idx):
            q = idx.resolve(ast.Call(func=dep.args[0], args=[], keywords=[]), info.get('cls'), qual)
            if not q or q not in idx.funcs or q in seen or q == qual:
                continue
            seen.add(q)
            self.deps(q, seen, ctx)
            if not _has_yield(idx.funcs[q]['node']):
                self.func(q, ctx, 1, frozenset())
                continue
            mark = len(self.tail)
            self.dep_via = idx.funcs[q]['name']
            self.func(q, ctx, 1, frozenset())
            self.dep_via, self.sink, self.no_fail = None, self.head, False
            part = self.tail[mark:]
            del self.tail[mark:]
            self.tail[0:0] = part             # dependencies unwind in reverse order


def timeline(entry_qual, idx, G, nid, cfg=None):
    """The route's side effects in order; the red ones get a squiggle on their line in the map. {} when it has none.

    {'items': [{k, label, cond, via, fn, file, line, code, node, risk?, n?}],
     'commit': 'found' | 'unknown' | 'none'}"""
    w = _Walk(idx, G, nid, cfg or {})
    base = {'cond': '', 'catch': [], 'loops': (), 'handling': None}
    try:
        w.deps(entry_qual, set(), base)
        w.sink = w.head
        w.func(entry_qual, base, 0, frozenset())
    except (RecursionError, ValueError, TypeError, AttributeError, KeyError):
        return {}      # code we can't follow: no strip rather than no page
    seq = w.head + w.tail + w.later
    if not any(e['k'] != 'fail' for e in seq):
        return {}
    has_commit = any(e['k'] == 'commit' for e in seq)
    has_write = any(e['k'] == 'write' for e in seq)
    for i, e in enumerate(seq):
        if e['k'] != 'out':
            continue
        after = [x for j, x in enumerate(seq) if j > i or (j < i and set(x.get('loops', ())) & set(e['loops']))]
        risk = list(dict.fromkeys(x['t'] for x in after if x['k'] == 'fail'))
        if any(x['k'] == 'commit' for x in after):
            risk.append('commit')
        if has_commit and risk:
            e['risk'] = risk
    items = []
    for e in seq:
        if e['k'] == 'fail':
            continue
        e = {k: v for k, v in e.items() if k != 'loops' and v not in (None, '')}
        prev = items[-1] if items else None
        if prev and all(prev.get(k) == e.get(k) for k in ('k', 'label', 'cond', 'via')):
            prev['n'] = prev.get('n', 1) + 1
            if e.get('risk'):
                prev['risk'] = list(dict.fromkeys(prev.get('risk', []) + e['risk']))
            continue
        items.append(e)
    return {'items': items, 'commit': 'found' if has_commit else ('unknown' if has_write else 'none')}
