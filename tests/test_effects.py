"""The side-effect strip under each route header (idea 5), checked on tests/effects_fixture.py.

Run: python3 -m unittest discover tests
"""
import json, os, re, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import effects_fixture  # noqa: E402

GEN = os.path.join(HERE, '..', 'scripts', 'flowsegul_gen.py')


class SideEffects(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        repo = effects_fixture.make(os.path.join(cls.tmp.name, 'fx'))
        out = repo + '.html'
        subprocess.run([sys.executable, GEN, '--repo', repo, '--out', out, '--no-open'],
                       check=True, capture_output=True)
        with open(out) as f:
            data = json.loads(re.search(r'^const DATA = (.*);$', f.read(), re.M).group(1))
        cls.eps = {e['method'].upper() + ' ' + e['path']: e['effects'] for e in data['graph']['endpoints']}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def strip(self, key):
        return [(i['label'], bool(i.get('risk')), i.get('cond', ''), i.get('via', ''))
                for i in self.eps[key]['items']]

    def test_email_before_dependency_commit_with_error_after(self):
        self.assertEqual(self.strip('POST /orders'), [
            ('add Order', False, '', ''),
            ('email', True, '', ''),
            ('notify_partner', True, '', ''),        # from "irreversible" in .flowsegul.json
            ('commit', False, '', 'get_db'),         # after `yield` in the Depends generator
        ])
        email = self.eps['POST /orders']['items'][1]
        self.assertEqual(email['risk'], ['ValueError', 'commit'])
        self.assertEqual(email['fn'], 'send_receipt')
        self.assertIn('smtp.sendmail', email['code'])

    def test_email_after_commit_only_on_some_paths(self):
        self.assertEqual(self.strip('POST /users'), [
            ('add User', False, '', ''), ('commit', False, '', ''), ('email', False, 'if notify', '')])

    def test_session_begin_block_and_background_task(self):
        self.assertEqual(self.strip('POST /payments/{payment_id}/refund'), [
            ('update Payment', False, '', ''), ('POST psp.example.com', True, '', ''),
            ('commit', False, '', ''), ('add_task send_receipt', False, '', '')])

    def test_no_commit_found_marks_nothing(self):
        fx = self.eps['DELETE /documents/{doc_id}']
        self.assertEqual(fx['commit'], 'unknown')
        self.assertEqual(self.strip('DELETE /documents/{doc_id}'), [
            ('delete Document', False, '', ''), ('delete file', False, '', '')])

    def test_queued_task_before_commit(self):
        self.assertEqual(self.strip('POST /reports/{user_id}'), [
            ('update User', False, '', ''), ('queue rebuild_report', True, '', ''), ('commit', False, '', '')])

    def test_type_alias_names_the_model(self):
        self.assertEqual(self.strip('PATCH /me')[0][0], 'add User')

    def test_read_only_route_has_no_strip(self):
        self.assertEqual(self.eps['GET /orders/{order_id}'], {})


if __name__ == '__main__':
    unittest.main()
