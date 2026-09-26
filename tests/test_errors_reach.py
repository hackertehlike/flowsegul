"""Errors travel the map (ideas 1+2) and reach (idea 3), checked on the shop fixture.

Run: python3 -m unittest discover tests
"""
import json, os, re, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import shop_fixture  # noqa: E402

GEN = os.path.join(HERE, '..', 'scripts', 'flowsegul_gen.py')


def endpoints(repo, *args):
    out = repo + '.html'
    subprocess.run([sys.executable, GEN, '--repo', repo, '--out', out, '--no-open', *args],
                   check=True, capture_output=True)
    with open(out) as f:
        html = f.read()
    data = json.loads(re.search(r'^const DATA = (.*);$', html, re.M).group(1))
    return {e['method'].upper() + ' ' + e['path']: e for e in data['graph']['endpoints']}


def node(ep, title):
    return next(n for n in ep['nodes'] if n['title'].endswith(title))


def step(ep, title, needle):
    return next(s for s in node(ep, title)['steps'] if needle in (s.get('expr') or ''))


class ErrorsAndReach(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        repo = os.path.join(cls.tmp.name, 'shop')
        shop_fixture.make(repo)
        cls.eps = endpoints(repo, '--changed', '--base', 'main')
        cls.all = endpoints(repo)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def codes(self, key, eps=None):
        return [(s['code'], s['kind'], s['new']) for s in (eps or self.eps)[key]['statuses']]

    def test_uncaught_error_becomes_new_500(self):
        self.assertEqual(self.codes('POST /orders/'), [('201', 'ok', False), ('500', 'bad', True)])
        ep = self.eps['POST /orders/']
        self.assertEqual(step(ep, 'place_order', 'apply_coupon')['esc'], ['ValueError'])
        self.assertEqual(step(ep, 'create_order', 'place_order')['esc'], ['ValueError'])
        self.assertTrue(node(ep, 'apply_coupon')['raises'][0]['esc'])

    def test_caught_error_maps_to_status(self):
        ep = self.eps['GET /orders/preview']
        self.assertEqual(self.codes('GET /orders/preview'), [('200', 'ok', False), ('400', 'http', False)])
        s = step(ep, 'preview', 'preview_price')
        self.assertNotIn('esc', s)
        self.assertEqual(s['caught'], [['ValueError', 400]])

    def test_http_exception_status_from_service(self):
        codes = self.codes('GET /orders/{order_id}', self.all)
        self.assertIn(('404', 'http', False), codes)
        self.assertNotIn('500', [c for c, _, _ in codes])

    def test_used_by_routes(self):
        used = node(self.eps['POST /orders/'], 'apply_coupon')['usedBy']
        self.assertEqual(sorted(used), ['GET /orders/preview', 'POST /orders/',
                                        'PUT /orders/carts/{cart_id}/coupon'])
        # a function only one route reaches gets no badge
        self.assertNotIn('usedBy', node(self.eps['POST /orders/'], 'create_order'))


if __name__ == '__main__':
    unittest.main()
