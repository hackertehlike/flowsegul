"""React mode: run the TypeScript analyzer on the checkout fixture and check what it finds.

Needs `node` and the `typescript` package (the repo's own, one next to flowsegul, the global npm
folder, or FLOWSEGUL_TS=<folder>). Without them these tests skip, so CI without Node stays green.
Run with: python3 -m unittest discover -s tests -v
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import react_fixture  # noqa: E402

SCRIPTS = os.path.join(HERE, '..', 'scripts')
ANALYZER = os.path.join(SCRIPTS, 'flowsegul_react.mjs')
GEN = os.path.join(SCRIPTS, 'flowsegul_gen.py')


def _ready():
    if not shutil.which('node'):
        return 'node not found'
    p = subprocess.run(['node', ANALYZER, '--check'], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True)
    return None if p.returncode == 0 else 'typescript not found (' + (p.stderr.strip() or 'check failed') + ')'


SKIP = _ready()


@unittest.skipIf(SKIP, SKIP or '')
class ReactAnalyzerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo = react_fixture.make(os.path.join(cls.tmp.name, 'shopweb'))
        p = subprocess.run(['node', ANALYZER, cls.repo], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True)
        assert p.returncode == 0, p.stderr
        cls.out = p.stdout
        cls.d = json.loads(p.stdout)
        cls.units = {u['id']: u for u in cls.d['units']}
        cls.rows = cls.d['rows']

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    # helpers
    def action(self, label):
        found = [a for a in self.d['actions'] if a['label'] == label]
        self.assertEqual(len(found), 1, [a['label'] for a in self.d['actions']])
        return found[0]

    def state(self, name):
        return next(s for s in self.d['states'] if s['name'] == name)

    def text(self, rid):
        return self.rows[rid]['t']

    def uname(self, rid):
        return self.units[self.rows[rid]['u']]['name']

    def test_deterministic(self):
        p = subprocess.run(['node', ANALYZER, self.repo], stdout=subprocess.PIPE, universal_newlines=True)
        strip = lambda s: re.sub(r'"ms":\d+', '', s)
        self.assertEqual(strip(p.stdout), strip(self.out))

    def test_labels(self):
        labels = [a['label'] for a in self.d['actions']]
        for want in ('click Apply', 'type in coupon', 'click Remove', 'page load', 'type in Note',
                     'click Clear note', 'click Clear cart', 'click Dark'):
            self.assertIn(want, labels)
        self.assertEqual(self.action('click Apply')['sub'], 'CouponField')
        self.assertEqual(self.action('page load')['sub'], 'CheckoutPage')
        self.assertEqual(self.action('click Apply')['group'], 'src/checkout')
        self.assertEqual(self.action('click Dark')['group'], 'src/settings')

    def test_setter_sites(self):
        cc = self.state('couponCode')
        self.assertEqual(len(cc['sites']), 2)
        self.assertEqual(sorted(self.text(r) for r in cc['sites']),
                         sorted(['<button onClick={() => onApply("")}>Remove</button>',
                                 '<button onClick={() => onApply(draft)}>Apply</button>']))
        self.assertEqual(cc['unc'], [])
        # the setter under its prop names, all highlighted
        hops = {self.text(r) for r in cc['hops']}
        self.assertIn('<PromoPanel onApply={setCouponCode} />', hops)
        self.assertIn('<CouponField onApply={onApply} />', hops)
        # through useCallback and a plain arrow: the wrappers are hops, their callers the sites
        note = self.state('note')
        self.assertEqual(sorted(self.text(r) for r in note['sites']),
                         sorted(['<textarea value={note} onChange={(e) => handleNote(e.target.value)} aria-label="Note" />',
                                 '<button onClick={clear}>Clear note</button>']))
        self.assertEqual({self.text(r) for r in note['hops']},
                         {'const handleNote = useCallback((v: string) => setNote(v), []);', 'const clear = () => setNote("");'})

    def test_apply_path_order(self):
        steps = self.action('click Apply')['steps']
        first = [self.text(s['r'][0]) for s in steps]
        self.assertEqual(first[:4], [
            '<button onClick={() => onApply(draft)}>Apply</button>',   # the handler
            '<CouponField onApply={onApply} />',                       # callback prop, one level up
            '<PromoPanel onApply={setCouponCode} />',                  # the setter, passed down
            'const [couponCode, setCouponCode] = useState("");',        # the state
        ])
        # then what reruns, then the effect that depends on it, its fetch, and the next setter
        self.assertTrue(steps[4].get('rr'))
        self.assertEqual(first[5], 'useEffect(() => {')
        self.assertEqual(self.uname(steps[5]['r'][0]), 'PriceLine')
        self.assertIn('fetch(`/orders/preview', first[6])
        self.assertEqual(first[7], '.then(setTotal);')
        self.assertEqual(first[8], 'const [total, setTotal] = useState<number>();')
        self.assertEqual(len(steps), 9)
        self.assertFalse(any(s.get('unc') for s in steps))

    def test_reruns(self):
        steps = self.action('click Apply')['steps']
        rr = {self.units[u]['name']: unc for s in steps[:5] for u, _, unc in s.get('rr', [])}
        self.assertEqual(set(rr), {'CheckoutPage', 'CartSummary', 'PriceLine', 'PromoPanel', 'CouponField',
                                   'NoteBox', 'Footer'})
        # Footer is memo() and gets no props: it may well not rerun
        self.assertTrue(rr['Footer'])
        self.assertFalse(rr['PriceLine'])
        self.assertTrue(self.units[next(u for u in self.units if self.units[u]['name'] == 'Footer')]['memo'])

    def test_fetch_chip(self):
        steps = self.action('click Apply')['steps']
        apis = [a for s in steps for r in s['r'] for a in self.rows[r].get('api', [])]
        self.assertEqual(apis, [['GET', '/orders/preview']])
        load = self.action('page load')['steps']
        self.assertIn(['GET', '/cart'], [a for s in load for r in s['r'] for a in self.rows[r].get('api', [])])

    def test_spread_is_uncertain(self):
        theme = self.state('theme')
        self.assertEqual(len(theme['sites']), 1)
        self.assertEqual(theme['unc'], theme['sites'])
        steps = self.action('click Dark')['steps']
        self.assertFalse(steps[0].get('unc'))
        self.assertTrue(all(s.get('unc') for s in steps[1:]))
        self.assertEqual(self.text(steps[1]['r'][0]), 'return <ThemeButton {...props} />;')
        # context hand-off is uncertain too
        items = self.state('items')
        self.assertEqual(len(items['sites']), 2)
        self.assertEqual([self.text(r) for r in items['unc']], ['<button onClick={() => setItems([])}>Clear cart</button>'])
        # ThemeButtons only spreads onPick on: not an action of its own
        self.assertFalse(any(a['label'].startswith('onPick') for a in self.d['actions']))


@unittest.skipIf(SKIP, SKIP or '')
class ReactPageTest(unittest.TestCase):
    def test_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = react_fixture.make(os.path.join(tmp, 'shopweb'))
            out = os.path.join(tmp, 'out.html')
            p = subprocess.run([sys.executable, GEN, '--repo', repo, '--out', out], cwd=repo,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            with open(out) as f:
                html = f.read()
            m = re.search(r'const DATA = (.*?);\n', html)
            self.assertIsNotNone(m)
            data = json.loads(m.group(1))
            react = data['graph']['react']
            self.assertIn('click Apply', [a['label'] for a in react['actions']])
            self.assertEqual(data['graph']['endpoints'], [])
            self.assertIn('id="stepbar"', html)


if __name__ == '__main__':
    unittest.main()
