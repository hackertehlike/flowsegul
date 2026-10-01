"""Functions stored as values and called later, checked on tests/dispatch_fixture.py: a registry of
tool families (dispatch edges) and functions handed to add_task / create_task / to_thread /
run_in_executor (deferred edges).

Run: python3 -m unittest discover tests
"""
import json, os, re, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import dispatch_fixture  # noqa: E402

GEN = os.path.join(HERE, '..', 'scripts', 'flowsegul_gen.py')


def endpoints(repo, *args):
    out = repo + '.html'
    subprocess.run([sys.executable, GEN, '--repo', repo, '--out', out, '--no-open', *args],
                   check=True, capture_output=True)
    with open(out) as f:
        data = json.loads(re.search(r'^const DATA = (.*);$', f.read(), re.M).group(1))
    return data['graph']['endpoints']


def by_key(eps):
    return {e['method'].upper() + ' ' + e['path']: e for e in eps if not e.get('other')}


def edges(ep, title):
    """{callee title: step} for the steps of one box."""
    n = next(n for n in ep['nodes'] if n['title'] == title)
    titles = {m['id']: m['title'] for m in ep['nodes']}
    return {titles[s['target']]: s for s in n['steps'] if s['target'] in titles}


class Dispatch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        repo = dispatch_fixture.make(os.path.join(cls.tmp.name, 'dx'))
        cls.all = by_key(endpoints(repo))
        cls.changed = endpoints(repo, '--changed', '--base', 'main')

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_registry_call_reaches_every_stored_function(self):
        for key in ('POST /chat', 'POST /chat/stream'):
            got = edges(self.all[key], 'call_tool')
            self.assertEqual(got['execute_tool']['disp'], [2, False])
            self.assertEqual(got['execute_tool_idempotent']['disp'], [2, False])
            self.assertNotIn('disp', got['family_for_tool'])     # the lookup itself is a proven call
            titles = [n['title'] for n in self.all[key]['nodes']]
            for t in ('_search', '_browse', '_send_draft'):
                self.assertIn(t, titles)

    def test_lambda_counts_but_draws_no_edge(self):
        # is_tool=mail_service.is_mail_tool and is_tool=lambda n: …: one of 2, one of them ours
        got = edges(self.all['POST /chat'], 'family_for_tool')
        self.assertEqual(list(got), ['is_mail_tool'])
        self.assertEqual(got['is_mail_tool']['disp'], [2, False])

    def test_dataclass_registry_with_positional_fields(self):
        got = edges(self.all['GET /chat/export'], 'export_rows')
        self.assertEqual(sorted(got), ['_to_csv', '_to_xlsx'])
        # register_exporter builds one from a parameter: "one of 2 or more"
        self.assertTrue(all(s['disp'] == [2, True] for s in got.values()))

    def test_add_task_is_a_later_edge(self):
        got = edges(self.all['POST /chat/files'], 'upload_local_context_files')
        s = got['store_uploaded_files']
        self.assertEqual(s['defer'], 'later')
        self.assertEqual(s['arg'], 'uploads=uploads, username=username')
        self.assertEqual([b['p'] for b in s['bind']], ['uploads', 'username'])
        self.assertIn('save', [n['title'] for n in self.all['POST /chat/files']['nodes']])

    def test_create_task_to_thread_and_executor(self):
        got = edges(self.all['POST /chat/summary'], 'summarize')
        self.assertEqual(got['refresh_title']['defer'], 'task')
        self.assertEqual(got['refresh_title']['ret'], '')
        self.assertEqual(got['render_pdf']['defer'], 'thread')
        self.assertEqual(got['archive']['defer'], 'thread')
        self.assertEqual(got['archive']['arg'], 'chat_id')

    def test_dispatched_errors_reach_the_route(self):
        # onedrive's execute_tool can raise ValueError: one of the targets, so a possible 500
        self.assertIn('500', [s['code'] for s in self.all['POST /chat']['statuses']])
        self.assertEqual(edges(self.all['POST /chat'], 'call_tool')['execute_tool'].get('esc'), ['ValueError'])

    def test_changed_tool_sits_under_the_routes_that_run_it(self):
        other = [e for e in self.changed if e.get('other')]
        self.assertEqual(other, [], [e['title'] for e in other])
        eps = by_key(self.changed)
        for key in ('POST /chat', 'POST /chat/stream'):
            n = next(n for n in eps[key]['nodes'] if n['title'] == 'execute_tool')
            self.assertTrue(n['changed'])
            self.assertIn('_show', [n['title'] for n in eps[key]['nodes']])
        files = [n['title'] for n in eps['POST /chat/files']['nodes'] if n['changed']]
        self.assertEqual(files, ['store_uploaded_files'])
        self.assertIn('enqueue', [n['title'] for n in eps['POST /chat/files']['nodes']])


if __name__ == '__main__':
    unittest.main()
