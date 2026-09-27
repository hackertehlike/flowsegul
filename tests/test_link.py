"""Frontend requests linked to the FastAPI routes they reach (tests/fullstack_fixture.py).

The analyzer part needs `node` and the `typescript` package, like tests/test_react.py; without them
those tests skip. The query-parameter tests run on Python alone.
Run with: python3 -m unittest discover -s tests -v
"""
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'scripts'))
import fullstack_fixture  # noqa: E402
import flowsegul_gen as gen  # noqa: E402
import flowsegul_shapes as shapes  # noqa: E402
from test_react import SKIP  # noqa: E402

GEN = os.path.join(HERE, '..', 'scripts', 'flowsegul_gen.py')


def page_data(html):
    m = re.search(r'const DATA = (.*?);\n', html)
    return json.loads(m.group(1))


@unittest.skipIf(SKIP, SKIP or '')
class LinkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        repo = fullstack_fixture.make(os.path.join(cls.tmp.name, 'fullshop'))
        out = os.path.join(cls.tmp.name, 'out.html')
        # a full-stack repo gets the React map on its own, no --react needed
        p = subprocess.run([sys.executable, GEN, '--repo', repo, '--out', out], cwd=repo,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        assert p.returncode == 0, p.stdout + p.stderr
        with open(out) as f:
            cls.html = f.read()
        d = page_data(cls.html)
        cls.eps = {e['id']: e for e in d['graph']['endpoints']}
        cls.react = d['graph']['react']
        cls.rows = cls.react['rows']
        cls.units = {u['id']: u for u in cls.react['units']}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def call(self, needle):
        """The request on the source line that contains `needle`."""
        found = [h for h in self.react['http'].values() if needle in self.rows[h['row']]['t']]
        self.assertEqual(len(found), 1, [self.rows[h['row']]['t'] for h in self.react['http'].values()])
        return found[0]

    def route(self, method, path):
        return next(e for e in self.eps.values() if e['method'].upper() == method and e['path'] == path)

    def to(self, h):
        e = self.eps[h['ln']['to']]
        return e['method'].upper() + ' ' + e['path']

    def test_wrapper_call_links(self):
        # api(`/orders/${id}`) → fetch("/api" + path); Vite's proxy strips /api
        h = self.call('api<{ id: number }>(`/orders/${id}`)')
        self.assertEqual((h['m'], h['p'], h['alt']), ('GET', '/api/orders/{}', '/orders/{}'))
        self.assertEqual(h['ln']['st'], 'ok')
        self.assertEqual(self.to(h), 'GET /orders/{order_id}')
        # the method comes from the init object the caller passes
        h = self.call('quantity: 1 })')
        self.assertEqual(self.to(h), 'POST /orders/')
        # the wrapper's own fetch line is not a request of its own
        self.assertFalse([h for h in self.react['http'].values() if 'fetch(BASE + path' in self.rows[h['row']]['t']])

    def test_named_client_functions(self):
        # usersApi.me() → http.get("/users/me") on an axios instance with baseURL "/api"
        h = self.call('usersApi.me()')
        self.assertEqual(self.to(h), 'GET /users/me')
        self.assertEqual(self.units[self.rows[h['row']]['u']]['name'], 'Avatar')
        # a client function nobody calls stays out, and its route has no caller
        self.assertTrue(self.route('DELETE', '/users/{user_id}').get('nocaller'))

    def test_shadowed_route(self):
        # /orders/{order_id} is declared before /orders/preview, so FastAPI sends "preview" there
        h = self.call('fetch(`/api/orders/preview?code=')
        ln = h['ln']
        self.assertEqual(ln['st'], 'shadow')
        self.assertEqual(self.to(h), 'GET /orders/{order_id}')
        self.assertEqual(self.eps[ln['meant']]['path'], '/orders/preview')
        # query keys are checked against the route it means: `n` isn't read, `item_count` is required
        self.assertEqual(ln.get('extra'), ['n'])
        self.assertEqual(ln.get('miss'), ['item_count'])

    def test_wrong_method(self):
        h = self.call('http.get(`/orders/${id}/refund`)')
        self.assertEqual(h['ln']['st'], 'method')
        self.assertEqual(h['ln']['want'], ['POST'])
        self.assertEqual(self.to(h), 'POST /orders/{order_id}/refund')

    def test_no_route(self):
        self.assertEqual(self.call('api(`/order/${id}/receipt`)')['ln']['st'], 'noroute')

    def test_trailing_slash_and_params(self):
        h = self.call('http.get("/orders", { params: { page: 2 } })')
        self.assertEqual(self.to(h), 'GET /orders/')
        self.assertTrue(h['ln'].get('slash'))
        self.assertEqual(h['q'], ['page'])
        self.assertNotIn('extra', h['ln'])
        self.assertNotIn('miss', h['ln'])      # page and size have defaults

    def test_request_param_reads_any_key(self):
        # search_users takes the Request: it may read `limit` itself, so no `no param` mark
        h = self.call('usersApi.search("ada")')
        self.assertEqual(h['q'], ['q', 'limit'])
        self.assertEqual(h['ln']['st'], 'ok')
        self.assertNotIn('extra', h['ln'])

    def test_unknown_and_external(self):
        self.assertEqual(self.call('fetch(src)')['ln']['st'], 'unres')
        self.assertEqual(self.call('api.github.com')['ln']['st'], 'ext')

    def test_callers(self):
        names = lambda e: sorted(self.units[self.rows[r]['u']]['name'] for r in e['callers'])
        self.assertEqual(names(self.route('GET', '/orders/{order_id}')), ['OrderPanel', 'OrderSummary', 'PriceLine'])
        self.assertEqual(names(self.route('POST', '/orders/{order_id}/refund')), ['RefundButton'])
        # PriceLine's call never reaches preview_order
        self.assertTrue(self.route('GET', '/orders/preview').get('nocaller'))
        self.assertEqual(names(self.route('GET', '/users/search')), ['OrderPanel'])

    def ty(self, needle):
        return [(t, lbl) for t, lbl, _ in self.call(needle)['ln'].get('ty', [])]

    def test_body_fields(self):
        # `qty` isn't a field of OrderIn (Pydantic drops it), so the required `quantity` is missing
        self.assertEqual(self.ty('qty: 2'), [('bad', 'OrderIn: no qty'), ('bad', 'needs quantity')])
        self.assertEqual(self.ty('http.post("/orders/")'), [('bad', 'needs OrderIn')])
        self.assertEqual(self.ty('quantity: 1'), [])

    def test_response_types(self):
        # the preview is read as a number; the route it means answers with an object
        self.assertEqual(self.ty('fetch(`/api/orders/preview'), [('bad', 'PreviewOut ≠ number')])
        # api<Order>(…): field by field, nested lists too; a null the type doesn't allow is grey
        self.assertEqual(self.ty('api<Order>'), [('bad', 'id: int ≠ string'), ('bad', 'no items[].quantity'),
                                                 ('dim', 'note: str | None ≠ string')])
        # `const data: { count: number } = await res.json()`
        self.assertEqual(self.ty('fetch("/api/users/stats")'), [('bad', 'count: str ≠ number')])
        # api<{ id: number }> only asks for what OrderOut has
        self.assertEqual(self.ty('api<{ id: number }>'), [])
        # the shapes are only for linking, not for the page
        self.assertFalse(any(k in h for h in self.react['http'].values() for k in ('b', 'r', 'nb', 'bx')))
        self.assertFalse(any(k in (e.get('route') or {}) for e in self.eps.values() for k in ('body', 'resp')))

    def test_page_has_link_ui(self):
        for s in ('function apiChips', 'function callerChips', 'function openCaller', 'function xBack'):
            self.assertIn(s, self.html)


class QueryParamsTest(unittest.TestCase):
    """Which query keys a route reads, and which it requires."""

    def facts(self, src, path='/x'):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, 'app'))
            with open(os.path.join(tmp, 'app', 'routes.py'), 'w') as f:
                f.write(textwrap.dedent(src))
            idx = gen.Index(tmp, gen.gather_py([tmp]))
            q = next(r[0] for r in idx.routes)
            return gen.query_params(idx.funcs[q], idx, path)

    def test_kinds(self):
        q, open_ = self.facts('''
            from typing import Annotated
            from fastapi import APIRouter, Body, Depends, Header, Query
            from pydantic import BaseModel

            router = APIRouter()

            class Item(BaseModel):
                name: str

            def paging(page: int = 1, size: int = 20):
                return page, size

            @router.post("/x/{item_id}")
            def f(item_id: int, item: Item, q: str, flag: bool = False,
                  tag: Annotated[str, Query(alias="t")] = "a", must: int = Query(...),
                  note: str = Body(""), agent: str = Header(""), p=Depends(paging)):
                pass
        ''', '/x/{item_id}')
        self.assertFalse(open_)
        self.assertEqual(q, {'q': True, 'flag': False, 't': False, 'must': True, 'page': False, 'size': False})

    def test_request_opens(self):
        q, open_ = self.facts('''
            from fastapi import APIRouter, Request
            router = APIRouter()

            @router.get("/x")
            def f(request: Request, q: str):
                pass
        ''')
        self.assertTrue(open_)
        self.assertEqual(q, {'q': True})

    def test_unknown_type_never_required(self):
        # SessionDep may be an alias we can't follow: never claim a caller must send it
        q, _ = self.facts('''
            from fastapi import APIRouter
            from somewhere import SessionDep
            router = APIRouter()

            @router.get("/x")
            def f(session: SessionDep, q: int):
                pass
        ''')
        self.assertEqual(q, {'session': False, 'q': True})


class ShapesTest(unittest.TestCase):
    """Body and response models as JSON shapes, and the marks a request gets against them."""

    SRC = '''
        import enum
        from typing import Annotated, Optional
        from fastapi import APIRouter, Body
        from pydantic import BaseModel, ConfigDict, Field
        from sqlmodel import Relationship, SQLModel

        router = APIRouter()

        class Kind(str, enum.Enum):
            a = "a"

        class Base(SQLModel):
            title: str = Field(max_length=10)
            kind: Kind = Kind.a

        class Item(Base, table=True):
            id: int = Field(default=None, primary_key=True)
            owner: "User" = Relationship(back_populates="items")
            secret: str = Field(exclude=True)

        class Strict(BaseModel):
            model_config = ConfigDict(extra="forbid")
            name: str = Field(..., alias="fullName")
            note: Optional[str] = None

        class Camel(BaseModel):
            model_config = ConfigDict(alias_generator=lambda s: s)
            x: int

        @router.post("/items")
        def create(item: Base) -> Item:
            pass

        @router.post("/two")
        def two(a: Strict, n: Annotated[int, Body()], tag: str = "x") -> list[Strict]:
            pass

        @router.post("/embed", response_model=None)
        def embed(s: Strict = Body(embed=True)):
            pass

        @router.put("/partial", response_model=Strict, response_model_exclude_none=True)
        def partial(c: Camel):
            pass
    '''

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.makedirs(os.path.join(cls.tmp.name, 'app'))
        with open(os.path.join(cls.tmp.name, 'app', 'routes.py'), 'w') as f:
            f.write(textwrap.dedent(cls.SRC))
        cls.idx = gen.Index(cls.tmp.name, gen.gather_py([cls.tmp.name]))
        cls.facts = {p: gen.route_facts(q, cls.idx, p, m) for q, m, p, *_ in cls.idx.routes}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def fields(self, sh):
        return {k: (v[0], v[1]) for k, v in sh['o'].items()}

    def test_model_input(self):
        b = self.facts['/items']['body']
        self.assertEqual((b['t'], b['req']), ('Base', True))
        self.assertEqual(self.fields(b['s']), {'title': ('str', 0), 'kind': ('str', 1)})

    def test_model_output(self):
        # inherited fields, no relationship, no excluded field; a str Enum is a string
        r = self.facts['/items']['resp']
        self.assertEqual(self.fields(r['s']), {'title': ('str', 0), 'kind': ('str', 0), 'id': ('num', 0)})

    def test_several_body_params(self):
        # FastAPI reads each from its own key; `tag` is a query key, not part of the body
        b = self.facts['/two']['body']
        self.assertEqual(set(b['s']['o']), {'a', 'n'})
        self.assertEqual(self.fields(b['s']['o']['a'][0]), {'fullName': ('str', 0), 'note': ({'u': ['str', 'null']}, 1)})
        self.assertEqual(self.facts['/two']['resp']['s']['a']['extra'], 'forbid')

    def test_embed_and_no_response(self):
        self.assertEqual(set(self.facts['/embed']['body']['s']['o']), {'s'})
        self.assertNotIn('resp', self.facts['/embed'])
        # fields left out of the response: can't say what it has
        self.assertNotIn('resp', self.facts['/partial'])
        self.assertTrue(self.facts['/partial']['body']['s'].get('alias'))

    def marks(self, h, path):
        return [(t, lbl) for t, lbl, _ in shapes.check_request(h, self.facts[path])]

    def test_check(self):
        o = lambda **kw: {'o': {k: [v[0], v[1], v[2] if len(v) > 2 else ''] for k, v in kw.items()}, 'n': ''}
        body = lambda sh: {'b': {'s': sh, 't': ''}}
        self.assertEqual(self.marks(body(o(title=('str', 0))), '/items'), [])
        # a key TypeScript marks optional may never be sent: grey
        self.assertEqual(self.marks(body(o(title=('str', 0), tilte=('str', 1))), '/items'), [('dim', 'Base: no tilte')])
        # optional in TypeScript, required by the model: grey
        self.assertEqual(self.marks(body(o(title=('str', 1, 'string'))), '/items'), [('dim', 'needs title')])
        # "3" into an int is read as 3, but 3 into a str is a 422
        self.assertEqual(self.marks(body(o(a=(o(fullName=('num', 0, 'number')), 0), n=('str', 0, 'string'))), '/two'),
                         [('bad', 'a.fullName: number ≠ str'), ('dim', 'n: string ≠ int')])
        # an open model (alias generator) gets no key marks
        self.assertEqual(self.marks(dict(body(o(y=('num', 0))), r={'s': o(x=('num', 0))}), '/partial'), [])

    def test_null_in_response(self):
        r = lambda sh: {'bx': 1, 'r': {'s': sh, 't': 'T'}}
        arr = lambda **kw: {'a': {'o': {k: [v, 0, 'string'] for k, v in kw.items()}, 'n': ''}}
        self.assertEqual(self.marks(r(arr(fullName='str', note='str')), '/two'), [('dim', '[].note: Optional[str] ≠ string')])
        # TypeScript without strict null checks: every type takes null
        self.assertEqual(self.marks(dict(r(arr(fullName='str', note='str')), lax=1), '/two'), [])
        # and a field the frontend reads that the model doesn't have
        self.assertEqual(self.marks(r(arr(fullName='str', email='str')), '/two'), [('bad', 'no [].email')])


class MatchTest(unittest.TestCase):
    def test_segments(self):
        self.assertTrue(gen._seg_match(['orders', '{}'], ['orders', '{order_id}']))
        self.assertTrue(gen._seg_match(['orders', 'preview'], ['orders', '{order_id}']))
        # a value known only at run time is not taken for the literal "me"
        self.assertFalse(gen._seg_match(['users', '{}'], ['users', 'me']))
        self.assertTrue(gen._seg_match(['files', 'a', 'b.txt'], ['files', '{path:path}']))
        self.assertFalse(gen._seg_match(['orders'], ['orders', '{order_id}']))


if __name__ == '__main__':
    unittest.main()
