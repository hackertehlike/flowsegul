"""Constant lookup: a function's code carries where each ALL_CAPS module-level name it reads is set,
and the page can mark every changed function in an endpoint viewed at once.

Run: python3 -m unittest discover tests
"""
import json, os, re, subprocess, sys, tempfile, textwrap, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GEN = os.path.join(HERE, '..', 'scripts', 'flowsegul_gen.py')
TEMPLATE = os.path.join(HERE, '..', 'scripts', 'template.html')

FILES = {
    'app/__init__.py': '',
    'app/core/__init__.py': '',
    'app/core/config.py': 'MAX_RETRIES = 3\n',
    'app/core/tools.py': 'ONEDRIVE_TOOL_NAMES = frozenset({\n    "onedrive_search",\n})\n',
    'app/core/reexport.py': 'from app.core.tools import ONEDRIVE_TOOL_NAMES\n',
    'app/service.py': textwrap.dedent('''\
        from app.core.reexport import ONEDRIVE_TOOL_NAMES
        from app.core import config

        LIMIT: int = 10


        def is_onedrive_tool(tool_name):
            MAX_LOCAL = 1
            return tool_name in ONEDRIVE_TOOL_NAMES and MAX_LOCAL


        def run(tool_name):
            return config.MAX_RETRIES + LIMIT if is_onedrive_tool(tool_name) else 0
        '''),
    'app/routes.py': textwrap.dedent('''\
        from fastapi import APIRouter
        from app.service import run

        router = APIRouter()


        @router.post("/run")
        def run_tool(name: str):
            return run(name)
        '''),
}


class Consts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        repo = os.path.join(self.tmp.name, 'shop')
        for rel, text in FILES.items():
            p = os.path.join(repo, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, 'w') as f:
                f.write(text)
        env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@e', GIT_COMMITTER_NAME='t',
                   GIT_COMMITTER_EMAIL='t@e')
        for a in (['init', '-q'], ['add', '-A'], ['commit', '-qm', 'init']):
            subprocess.check_call(['git', '-C', repo, *a], env=env)
        out = os.path.join(self.tmp.name, 'out.html')
        subprocess.check_call([sys.executable, GEN, '--repo', repo, '--out', out], cwd=repo, env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(out) as f:
            data = json.loads(re.search(r'const DATA = (.*?);\n', f.read()).group(1))
        self.code = {v['name']: v for v in data['code'].values()}

    def tearDown(self):
        self.tmp.cleanup()

    def test_imported_constant_is_found_through_a_reexport(self):
        c = self.code['is_onedrive_tool']['consts']
        self.assertEqual(c['ONEDRIVE_TOOL_NAMES']['file'], 'app/core/tools.py')
        self.assertEqual(c['ONEDRIVE_TOOL_NAMES']['line'], 1)
        self.assertIn('"onedrive_search"', c['ONEDRIVE_TOOL_NAMES']['src'])
        self.assertNotIn('MAX_LOCAL', c)   # the function's own variable, not a module constant

    def test_module_attribute_and_annotated_constant(self):
        c = self.code['run']['consts']
        self.assertEqual(c['config.MAX_RETRIES']['src'], 'MAX_RETRIES = 3')
        self.assertEqual(c['LIMIT']['src'], 'LIMIT: int = 10')

    def test_no_constants_no_key(self):
        self.assertNotIn('consts', self.code['run_tool'])


class Page(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(TEMPLATE) as f:
            cls.t = f.read()

    def test_constants_are_marked_in_code(self):
        self.assertIn('function markConsts(pre,consts)', self.t)
        self.assertIn('markConsts(pre,c.consts);', self.t)

    def test_mark_all_viewed_for_an_endpoint(self):
        for needle in ('id="vwall"', 'function markAllViewed', 'function setViewedMany',
                       "k==='V'&&e.shiftKey"):
            self.assertIn(needle, self.t)


if __name__ == '__main__':
    unittest.main()
