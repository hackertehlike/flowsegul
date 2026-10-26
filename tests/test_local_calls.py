"""A bare-name call to a local value (a callback parameter, a variable, a loop target…) is not a
call to some other file's function of that name; a bare name only reaches a def in its own file or
one it imports. Checked on tests/callback_fixture.py.

Run: python3 -m unittest discover tests
"""
import ast, json, os, re, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'scripts'))
import callback_fixture  # noqa: E402
import flowsegul_gen as gen  # noqa: E402

GEN = os.path.join(HERE, '..', 'scripts', 'flowsegul_gen.py')
SVC = 'app/services/calendar_service.py'


def page(repo, *args):
    out = repo + '.html'
    subprocess.run([sys.executable, GEN, '--repo', repo, '--out', out, '--no-open', *args],
                   check=True, capture_output=True)
    with open(out) as f:
        data = json.loads(re.search(r'^const DATA = (.*);$', f.read(), re.M).group(1))
    return {e['method'].upper() + ' ' + e['path']: e for e in data['graph']['endpoints']}


def calls(ep, title):
    n = next(n for n in ep['nodes'] if n['title'] == title)
    ids = {m['id']: m['title'] for m in ep['nodes']}
    return [ids.get(s['target'], s['target']) for s in n['steps']]


class LocalCalls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo = callback_fixture.make(os.path.join(cls.tmp.name, 'cb'))
        cls.idx = gen.Index(cls.repo, gen.gather_py([cls.repo]))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def targets(self, rel, name):
        """{called name: qual it resolves to} for every bare-name call in a function's body."""
        qual = f'{rel}::{name}'
        fn = self.idx.funcs[qual]['node']
        return {c.func.id: self.idx.resolve(c, None, qual) for s in fn.body for c in ast.walk(s)
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}

    def test_callback_parameter_is_not_another_files_function(self):
        self.assertEqual(self.targets(SVC, 'runner'), {'write': None})
        self.assertIsNone(self.targets(SVC, '_write_to_event')['write'])

    def test_same_file_function_still_resolves(self):
        self.assertEqual(self.targets(SVC, '_write_to_event')['_audit'], f'{SVC}::_audit')

    def test_every_kind_of_local_is_a_value(self):
        got = self.targets(SVC, 'every_kind_of_local')
        self.assertEqual({k: v for k, v in got.items() if v}, {})
        for name in ('write', 'render', 'draw', 'fill', 'save', 'scale', 'close', 'flush', 'shape',
                     'stamp', '_audit', 'apply', 'emit'):
            self.assertIn(name, got)

    def test_bare_name_without_import_does_not_cross_files(self):
        self.assertEqual(self.targets(SVC, 'unimported'), {'draw': None})

    def test_imports_still_resolve(self):
        self.assertEqual(self.targets('app/api/slides.py', 'fill'), {'fill_slide': 'app/pptx/fill.py::write'})
        self.assertEqual(self.targets('app/api/slides.py', 'shape'), {'draw': 'app/pptx/shapes.py::draw'})

    def test_route_graph_has_no_edge_to_the_unrelated_write(self):
        subprocess.run(['git', '-C', self.repo, 'checkout', '-q', 'feat'], check=True)
        eps = page(self.repo)
        self.assertEqual(calls(eps['POST /calendar/events/{event_id}'], 'runner'), [])
        self.assertNotIn('write', [n['title'] for n in eps['POST /calendar/events/{event_id}']['nodes']])
        self.assertIn('write', calls(eps['POST /slides/fill'], 'fill'))

    def test_changed_write_does_not_pull_in_calendar_routes(self):
        subprocess.run(['git', '-C', self.repo, 'checkout', '-q', 'feat'], check=True)
        eps = page(self.repo, '--changed', '--base', 'main')
        self.assertIn('POST /slides/fill', eps)
        self.assertNotIn('POST /calendar/events/{event_id}', eps)
        self.assertNotIn('DELETE /calendar/events/{event_id}', eps)


if __name__ == '__main__':
    unittest.main()
