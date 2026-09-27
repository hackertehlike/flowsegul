"""Mismatched types where a value is handed from one function to another, on the types fixture.

Run: python3 -m unittest discover tests
"""
import json, os, re, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'scripts'))
import types_fixture  # noqa: E402
import flowsegul_types as ft  # noqa: E402

GEN = os.path.join(HERE, '..', 'scripts', 'flowsegul_gen.py')


def endpoints(repo, *args):
    out = repo + '.html'
    subprocess.run([sys.executable, GEN, '--repo', repo, '--out', out, *args], check=True, capture_output=True)
    with open(out) as f:
        html = f.read()
    data = json.loads(re.search(r'^const DATA = (.*);$', html, re.M).group(1))
    return {e['method'].upper() + ' ' + e['path']: e for e in data['graph']['endpoints']}


def marks(ep):
    """[(box, where, at, 'got ≠ want', dim)] for every type mark in an endpoint's map."""
    out = []
    for n in ep['nodes']:
        for s in n['steps']:
            out += [(n['title'], t['where'], t['at'], t['got'] + ' ≠ ' + t['want'], t['dim']) for t in s.get('tm', [])]
        out += [(n['title'], 'row', t['at'], t['got'] + ' ≠ ' + t['want'], t['dim']) for t in n.get('types', [])]
    return sorted(out)


class MismatchedTypes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo = types_fixture.make(os.path.join(cls.tmp.name, 'typeshop'))
        cls.eps = endpoints(cls.repo)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_schema_field_into_uuid_param(self):
        m = marks(self.eps['POST /customers/'])
        self.assertIn(('create_customer', 'call', 'payload.customer_id', 'str ≠ UUID', False), m)
        # the repository can return None, and load() hands it back as a Customer
        self.assertIn(('CustomerService.load', 'ret', 'self.repo.get(customer_id)', 'Customer | None ≠ Customer', False), m)
        # a route's return value is turned into its response model by FastAPI: Customer for CustomerOut is fine
        self.assertEqual(len(m), 2)

    def test_checked_none_is_fine(self):
        # `if customer is None: raise …` rules it out; jwt.encode gives text; `extra: dict = None` takes None
        self.assertEqual(marks(self.eps['GET /customers/{customer_id}']), [])

    def test_maybe_checked_none_is_grey(self):
        m = marks(self.eps['GET /customers/{customer_id}/email'])
        # `if customer: print(…)` tests it but doesn't stop: grey on the line and on the arrow it came back on
        self.assertIn(('CustomerService.email_of', 'row', 'customer.email', 'Customer | None ≠ Customer', True), m)
        self.assertIn(('CustomerService.email_of', 'ret', '', 'Customer | None ≠ Customer', True), m)

    def test_bytes_and_env(self):
        m = marks(self.eps['POST /customers/{customer_id}/notes'])
        self.assertIn(('add_note', 'call', 'data', 'bytes ≠ str', False), m)             # save_note(…, data)
        self.assertIn(('add_note', 'call', 'key', 'str | None ≠ str', False), m)         # os.getenv
        self.assertIn(('add_note', 'here', 'data', 'bytes ≠ str', False), m)             # f"note {data}"

    def test_route_returning_none(self):
        m = marks(self.eps['GET /customers/{customer_id}/raw'])
        self.assertEqual(m, [('raw_customer', 'ret', 'service.find(customer_id)', 'None ≠ CustomerOut', False)])

    def test_mypy_output_is_reused(self):
        def where(rel, needle, token):   # mypy's 1-based line and column of `token` in the line with `needle`
            with open(os.path.join(self.repo, rel)) as f:
                return next((i, l.index(token) + 1) for i, l in enumerate(f.read().splitlines(), 1) if needle in l)
        desc, dcol = where('app/api/customers.py', 'service.describe(customer)', 'customer)')
        email, ecol = where('app/services/customers.py', 'return customer.email', 'customer')
        saved = os.path.join(self.tmp.name, 'mypy.txt')
        with open(saved, 'w') as f:
            f.write(f'app/api/customers.py:{desc}:{dcol}: error: Argument 1 to "describe" of "CustomerService" '
                    f'has incompatible type "Customer"; expected "CustomerOut"  [arg-type]\n'
                    f'app/services/customers.py:{email}:{ecol}: error: Item "None" of "Customer | None" has no '
                    f'attribute "email"  [union-attr]\n'
                    f'app/api/customers.py:1:1: error: Name "x" is not defined  [name-defined]\n')
        eps = endpoints(self.repo, '--types', saved)
        self.assertIn(('get_customer', 'call', 'customer', 'Customer ≠ CustomerOut', False),
                      marks(eps['GET /customers/{customer_id}']))
        # mypy agrees with the grey mark, so it turns red
        self.assertIn(('CustomerService.email_of', 'row', 'customer.email', 'Customer | None ≠ Customer', False),
                      marks(eps['GET /customers/{customer_id}/email']))


class ChangedView(unittest.TestCase):
    """In the PR view an unchecked None is only marked on lines the branch changed."""
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        repo = types_fixture.make(os.path.join(cls.tmp.name, 'typeshop'))
        run = lambda *a: subprocess.run(['git', *a], cwd=repo, check=True, capture_output=True)
        run('checkout', '-q', '-b', 'feature')
        path = os.path.join(repo, 'app/services/customers.py')
        with open(path) as f:
            src = f.read()
        # edit email_of's return line, and load's signature only: its `return self.repo.get(...)` stays
        src = src.replace('return customer.email', 'return customer.email.lower()')
        src = src.replace('    def load(self, customer_id: UUID) -> Customer:',
                          '    def load(self, customer_id: UUID) -> Customer:  # loads one')
        with open(path, 'w') as f:
            f.write(src)
        run('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qam', 'change')
        cls.eps = endpoints(repo, '--changed', '--base', 'main')

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_only_new_none_marks(self):
        m = marks(self.eps['POST /customers/'])
        # str ≠ UUID is not about a None: always shown
        self.assertIn(('create_customer', 'call', 'payload.customer_id', 'str ≠ UUID', False), m)
        # load's None hand-off is on a line the branch didn't touch
        self.assertFalse([x for x in m if 'None' in x[3]])
        m = marks(self.eps['GET /customers/{customer_id}/email'])
        # the edited line keeps its (grey) mark
        self.assertIn(('CustomerService.email_of', 'row', 'customer.email', 'Customer | None ≠ Customer', True), m)


class Parsing(unittest.TestCase):
    def test_type_text(self):
        self.assertEqual(ft.members('Optional[Customer]'), ['Customer', 'None'])
        self.assertEqual(ft.members('uuid.UUID | None'), ['UUID', 'None'])
        self.assertEqual(ft.members('Annotated[str, Depends(x)]'), ['str'])
        self.assertEqual(ft.pretty('Optional[uuid.UUID]'), 'UUID | None')
        self.assertEqual(ft.pretty('Union[int, str, None]'), 'int | str | None')

    def test_pyright_json(self):
        out = ft.parse_type_output(json.dumps({'generalDiagnostics': [
            {'file': '/r/app/a.py', 'severity': 'error', 'rule': 'reportArgumentType',
             'message': 'Argument of type "str | None" cannot be assigned to parameter "key" of type "str"',
             'range': {'start': {'line': 4, 'character': 9}}},
            {'file': '/r/app/a.py', 'severity': 'error', 'rule': 'reportOptionalMemberAccess',
             'message': '"email" is not a known attribute of "None"', 'range': {'start': {'line': 9, 'character': 1}}},
            {'file': '/r/app/a.py', 'severity': 'error', 'rule': 'reportUndefinedVariable',
             'message': '"x" is not defined', 'range': {'start': {'line': 1, 'character': 0}}}]}), '/r')
        self.assertEqual(sorted(out['app/a.py']), [5, 10])
        self.assertEqual((out['app/a.py'][5][0]['got'], out['app/a.py'][5][0]['want']), ('str | None', 'str'))


if __name__ == '__main__':
    unittest.main()
