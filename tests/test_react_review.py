"""React review checks: missing dep, read by N, no loading / error state, and state that clears.

Runs the analyzer on tests/react_review_fixture.py (two commits: an orders page, then a PR that
moves the coupon field under a promo panel that can close). Skips without node + typescript.
Run with: python3 -m unittest discover -s tests -v
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'scripts'))
import react_review_fixture  # noqa: E402
from test_react import SKIP, ANALYZER, GEN  # noqa: E402


def analyze(root):
    p = subprocess.run(['node', ANALYZER, root], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def read(path):
    with open(path) as f:
        return f.read()


def page_data(html):
    return json.loads(re.search(r'const DATA = (.*?);\n', html).group(1))['graph']['react']


@unittest.skipIf(SKIP, SKIP or '')
class ReactReviewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo = react_review_fixture.make(os.path.join(cls.tmp.name, 'ordersweb'))
        cls.d = analyze(cls.repo)
        cls.units = {u['id']: u for u in cls.d['units']}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    # the rows (with their unit's name) that carry a check of kind k
    def flagged(self, k, d=None):
        d = d or self.d
        units = {u['id']: u for u in d['units']}
        out = []
        for rid, r in d['rows'].items():
            for f in r.get('f', []):
                if f[0] == k:
                    out.append((units[r['u']]['name'], r['t'], f, rid))
        return out

    def test_missing_dep(self):
        deps = self.flagged('dep')
        self.assertEqual(len(deps), 1, deps)
        name, text, f, rid = deps[0]
        self.assertEqual((name, text, f[1]), ('PriceLine', '}, [couponCode]);', 'missing dep'))
        self.assertTrue(f[2].startswith('items.length\n'), f[2])
        self.assertIn(['dep', rid], self.d['problems'])
        # a squiggle under the read, on the fetch line
        fetch = next(r for r in self.d['rows'].values() if r['t'].startswith('fetch(`/orders/preview'))
        q = [m for m in fetch['m'] if m[2] == 'q']
        self.assertEqual(len(q), 1)
        self.assertEqual(fetch['t'][q[0][0]:q[0][1]], 'items')
        # OrdersPage's effect reads a literal, a ref and a helper that only sets state: all fine
        self.assertFalse([x for x in deps if x[0] == 'OrdersPage'])

    def test_read_by(self):
        reads = self.flagged('read')
        self.assertEqual([(n, f[1]) for n, _, f, _ in reads], [('OrdersPage', 'read by 2')])
        self.assertEqual(reads[0][2][2].split('\n')[0].split()[0], 'Badge')
        self.assertFalse([p for p in self.d['problems'] if p[0] == 'read'])

    def test_loading_and_error(self):
        load = {(n, t.split('=')[0].strip()): f for n, t, f, _ in self.flagged('load')}
        err = {(n, t.split('=')[0].strip()): f for n, t, f, _ in self.flagged('err')}
        orders = ('OrdersPage', 'const { data }')
        self.assertIn(orders, load)                # through the useOrders hook, data.length with no check
        self.assertIn(orders, err)
        self.assertIn('data.length', load[orders][2])
        tags = ('OrdersPage', 'const { data: tags }')
        self.assertEqual(load[tags][3:], [1])      # `tags ?? []`: faded, not a problem
        self.assertIn(tags, err)
        self.assertFalse([k for k in load if 'stats' in k[1]])   # checks isLoading and error
        price = [k for k in load if k[0] == 'PriceLine']
        self.assertEqual(len(price), 1)            # the fetch in the effect, stored with .then(setTotal)
        self.assertEqual(len(self.d['problems']), len({tuple(p) for p in self.d['problems']}))
        faded_rows = {rid for _, _, f, rid in self.flagged('load') if f[3:] == [1]}
        self.assertFalse([p for p in self.d['problems'] if p[0] == 'load' and p[1] in faded_rows])

    def test_state_that_clears(self):
        clr = {f[1]: (n, t, f) for n, t, f, _ in self.flagged('clr')}
        self.assertEqual(set(clr), {'n lost every render', 'open lost every render'})   # no diff given: only the always-bad ones
        self.assertEqual(clr['n lost every render'][1], 'const [n, setN] = useState(0);')    # Counter is declared inside Badge
        self.assertIn('<Counter />', clr['n lost every render'][2][2])
        self.assertEqual(clr['open lost every render'][1], '<Tip key={Math.random()} />')
        # every component with state says what unmounts it
        coupon = next(u for u in self.d['units'] if u['name'] == 'CouponField')
        self.assertIn('showPromo', [k for k, _ in coupon['mount']])

    def test_draft_clears_against_base(self):
        out = os.path.join(self.tmp.name, 'page.html')
        p = subprocess.run([sys.executable, GEN, '--repo', self.repo, '--react', '--from', 'HEAD~1', '--out', out],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        d = page_data(read(out))
        clr = {f[1]: (n, t, f, rid) for n, t, f, rid in self.flagged('clr', d)}
        self.assertIn('draft lost if !showPromo', clr)
        n, t, f, rid = clr['draft lost if !showPromo']
        self.assertEqual((n, t), ('CouponField', 'const [draft, setDraft] = useState("");'))
        self.assertIn('{showPromo && <CouponField onApply={onApply} />}', f[2])
        self.assertIn(['clr', rid], d['problems'])
        # PriceLine and Badge sit where they were: nothing new unmounts them
        self.assertEqual(set(clr), {'draft lost if !showPromo', 'n lost every render', 'open lost every render'})

    def test_lost_when(self):
        from flowsegul_gen import lost_when
        self.assertEqual(lost_when('showPromo'), 'if !showPromo')
        self.assertEqual(lost_when('!open'), 'if open')
        self.assertEqual(lost_when('if stats.isLoading return'), 'if stats.isLoading')
        self.assertEqual(lost_when('a > 1'), 'if !(a > 1)')
        self.assertEqual(lost_when('key={id}'), 'when key changes')

    def test_same_base_adds_nothing(self):
        import flowsegul_gen
        head = analyze(self.repo)
        before = json.dumps(head, sort_keys=True)
        flowsegul_gen.mark_new_clears(head, analyze(self.repo))
        self.assertEqual(json.dumps(head, sort_keys=True), before)

    def test_page_draws_checks(self):
        out = os.path.join(self.tmp.name, 'page2.html')
        subprocess.run([sys.executable, GEN, '--repo', self.repo, '--react', '--out', out],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        html = read(out)
        for s in ('function chkChip(', "'grp sect','Problems", 'class="rsq"', '.rc.chk.unc'):
            self.assertIn(s, html)


if __name__ == '__main__':
    unittest.main()
