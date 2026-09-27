"""Data behind "ask a box" and click-to-trace, checked on the shop fixture.

Run: python3 -m unittest discover tests
"""
import os, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import shop_fixture  # noqa: E402
from test_errors_reach import endpoints, node  # noqa: E402

TEMPLATE = os.path.join(HERE, '..', 'scripts', 'template.html')


class AskData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        repo = os.path.join(cls.tmp.name, 'shop')
        shop_fixture.make(repo)
        cls.eps = endpoints(repo)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_raise_carries_the_status_the_client_gets(self):
        r = node(self.eps['GET /orders/{order_id}'], 'OrderService.get_order')['raises'][0]
        self.assertEqual((r['t'], r['st'], r['esc']), ('HTTPException', '404', False))
        # an error with no status of its own: no `st`, the page answers 500 unless a caller catches it
        r = node(self.eps['POST /orders/'], 'apply_coupon')['raises'][0]
        self.assertEqual((r['t'], r['esc']), ('ValueError', True))
        self.assertNotIn('st', r)
        self.assertNotIn('here', r)

    def test_raise_inside_except_is_not_marked_handled_here(self):
        # `raise HTTPException(400)` in preview's except block is what the client gets
        r = node(self.eps['GET /orders/preview'], 'preview')['raises'][0]
        self.assertEqual(r['st'], '400')

    def test_aliases_exported_for_tracing(self):
        # compute_total walks `for i in items`: i is an element of items
        self.assertEqual(node(self.eps['POST /orders/'], 'compute_total').get('alias'), {'i': ['items', '[]']})

    def test_page_has_the_ask_ui(self):
        with open(TEMPLATE) as f:
            t = f.read()
        for needle in ('class="askbtn"', 'id="asklist"', 'id="askpop"', 'function traceFrom',
                       'function traceFail', 'function traceRoutes', 'function usesOfVar'):
            self.assertIn(needle, t)


if __name__ == '__main__':
    unittest.main()
