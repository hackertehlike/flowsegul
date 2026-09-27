"""React review: a state that a diff starts throwing away (`draft erased when showPromo is false`).

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

    def test_nothing_marked_without_a_diff(self):
        self.assertFalse(self.flagged('clr'))
        self.assertNotIn('problems', self.d)
        self.assertFalse([r for r in self.d['rows'].values() if r.get('f') or any(m[2] == 'q' for m in r.get('m', []))])
        # every component with state says what unmounts it, for the comparison
        coupon = next(u for u in self.d['units'] if u['name'] == 'CouponField')
        self.assertIn('showPromo', [k for k, _ in coupon['mount']])

    def test_draft_clears_against_base(self):
        out = os.path.join(self.tmp.name, 'page.html')
        p = subprocess.run([sys.executable, GEN, '--repo', self.repo, '--react', '--from', 'HEAD~1', '--out', out],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        d = page_data(read(out))
        clr = {f[1]: (n, t, f, rid) for n, t, f, rid in self.flagged('clr', d)}
        self.assertIn('draft erased when showPromo is false', clr)
        n, t, f, rid = clr['draft erased when showPromo is false']
        self.assertEqual((n, t), ('CouponField', 'const [draft, setDraft] = useState("");'))
        self.assertIn('{showPromo && <CouponField onApply={onApply} />}', f[2])
        self.assertIn(['clr', rid], d['problems'])
        # PriceLine and Badge sit where they were: nothing new unmounts them
        self.assertEqual(set(clr), {'draft erased when showPromo is false'})

    def test_lost_when(self):
        from flowsegul_gen import lost_when
        self.assertEqual(lost_when('showPromo'), 'when showPromo is false')
        self.assertEqual(lost_when('!open'), 'when open is true')
        self.assertEqual(lost_when('if stats.isLoading return'), 'when stats.isLoading is true')
        self.assertEqual(lost_when('a > 1'), 'when a > 1 is false')
        self.assertEqual(lost_when('key={id}'), 'when its key changes')

    def test_same_base_adds_nothing(self):
        import flowsegul_gen
        head = analyze(self.repo)
        before = json.dumps(head, sort_keys=True)
        flowsegul_gen.mark_new_clears(head, analyze(self.repo))
        self.assertEqual(json.dumps(head, sort_keys=True), before)

    def test_page_draws_the_mark(self):
        out = os.path.join(self.tmp.name, 'page2.html')
        subprocess.run([sys.executable, GEN, '--repo', self.repo, '--react', '--from', 'HEAD~1', '--out', out],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        html = read(out)
        for s in ('function chkChip(', "'grp sect','Problems"):
            self.assertIn(s, html)


if __name__ == '__main__':
    unittest.main()
