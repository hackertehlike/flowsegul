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
        h = self.call('api("/orders/", { method: "POST"')
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
        self.assertEqual(names(self.route('GET', '/orders/{order_id}')), ['OrderPanel', 'PriceLine'])
        self.assertEqual(names(self.route('POST', '/orders/{order_id}/refund')), ['RefundButton'])
        # PriceLine's call never reaches preview_order
        self.assertTrue(self.route('GET', '/orders/preview').get('nocaller'))
        self.assertEqual(names(self.route('GET', '/users/search')), ['OrderPanel'])

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
