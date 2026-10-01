"""Review hints on code lines (query per iteration, keyword the model doesn't have, a defaulted field
probably forgotten), checked on tests/hints_fixture.py.

Run: python3 -m unittest discover tests
"""
import json, os, re, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import hints_fixture  # noqa: E402

GEN = os.path.join(HERE, '..', 'scripts', 'flowsegul_gen.py')


class ReviewHints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        repo = hints_fixture.make(os.path.join(cls.tmp.name, 'hints'))
        out = repo + '.html'
        subprocess.run([sys.executable, GEN, '--repo', repo, '--out', out, '--no-open'],
                       check=True, capture_output=True)
        with open(out) as f:
            data = json.loads(re.search(r'^const DATA = (.*);$', f.read(), re.M).group(1))
        cls.code = data['code']
        cls.nodes = {}
        for e in data['graph']['endpoints']:
            for n in e['nodes']:
                cls.nodes.setdefault(n['title'], n)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def hints(self, title):
        """[(the hinted code line, kind, hover text, sure)] for a box."""
        n = self.nodes[title]
        lines = self.code[n['fnKey']]['code'].split('\n')
        return [(lines[h[0]].strip(), h[1], h[3], h[4]) for h in n.get('hints', [])]

    def test_chat_files_named(self):
        self.assertEqual(self.hints('_chat_files_named'), [
            ('message = await db_access.message.get_message_by_id(message_id=row.message_id)',
             'n1', 'query per iteration (N+1)', 1),
            ('size=row.size,', 'kw', '`size` is not a field of ChatFile, silently dropped', 1),
            ('ChatFile(', 'miss', 'row has extraction_status; not passed (defaults to READY); '
                                  'ChatService.get_files passes it', 0),
        ])

    def test_loop_iterable_runs_once(self):
        # the `for row in await …` line itself queries once; only the body runs per row
        self.assertNotIn('n1', [h[1] for h in self.hints('ChatService.get_files')])
        self.assertFalse(any(h[0].startswith('for row') for h in self.hints('_chat_files_named')))

    def test_fixed_literal_loop_is_not_n_plus_1(self):
        self.assertEqual(self.hints('summary'), [])   # also: ChatSummary leaves `pinned` out, no evidence

    def test_copied_object_evidence_only(self):
        # FileRef copies from row: DbFile has `size` (hint) but no `preview` (left alone)
        self.assertEqual(self.hints('file_refs'), [
            ('return [FileRef(name=row.name, message_id=row.message_id) for row in rows]', 'miss',
             'row has size; not passed (defaults to None)', 0)])

    def test_other_call_evidence_only(self):
        self.assertEqual(self.hints('chat_file_for'), [
            ('return ChatFile(name=name, message_id=message_id, message_role=None)', 'miss',
             'extraction_status not passed (defaults to READY); ChatService.get_files passes it', 0)])

    def test_forbid_dataclass_kwargs_and_own_init(self):
        # extra="forbid" and a dataclass raise; **kwargs and a class with its own __init__ are skipped
        self.assertEqual(self.hints('odd_calls'), [
            ('StrictFile(name=name, size=1),', 'kw', '`size` is not a field of StrictFile, raises at runtime', 1),
            ('Notice(text=name, urgent=True),', 'kw', '`urgent` is not a field of Notice, raises at runtime', 1),
        ])


if __name__ == '__main__':
    unittest.main()
