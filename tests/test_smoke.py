"""Smoke test: build a tiny FastAPI repo and run flowsegul_gen.py against it.

Run with: python3 -m unittest discover -s tests -v
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest

GEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'scripts', 'flowsegul_gen.py')

ROUTES = '''\
from fastapi import APIRouter
from app.service import list_items, create_item

router = APIRouter(prefix="/items")


@router.get("/")
def get_items():
    return list_items()


@router.post("/")
def post_item(name: str):
    return create_item(name)
'''

SERVICE = '''\
def list_items():
    return [normalize("a"), normalize("b")]


def create_item(name):
    return normalize(name)


def normalize(name):
    return name.strip()
'''

# the branch change: create_item now validates before normalizing
SERVICE_CHANGED = SERVICE.replace(
    'def create_item(name):\n    return normalize(name)',
    'def create_item(name):\n    if not name:\n        raise ValueError("empty")\n    return normalize(name)')


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(text)


class SmokeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, 'shop')
        os.makedirs(self.repo)
        self.env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.com',
                        GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@example.com')
        self.git('init', '-q')
        self.git('checkout', '-q', '-b', 'main')
        write(os.path.join(self.repo, 'app', '__init__.py'), '')
        write(os.path.join(self.repo, 'app', 'routes.py'), ROUTES)
        write(os.path.join(self.repo, 'app', 'service.py'), SERVICE)
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'initial')

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        subprocess.check_call(['git', '-C', self.repo, *args], env=self.env)

    def run_gen(self, *extra):
        out = os.path.join(self.tmp.name, 'out.html')
        proc = subprocess.run([sys.executable, GEN, '--repo', self.repo, '--out', out, *extra],
                              cwd=self.repo, env=self.env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(os.path.isfile(out), 'no HTML written')
        with open(out) as f:
            html = f.read()
        self.assertIn('<title>', html)
        self.assertIn('<script', html)
        self.assertNotIn('__DATA__', html)
        m = re.search(r'const DATA = (.*?);\n', html)
        self.assertIsNotNone(m, 'DATA blob not injected')
        data = json.loads(m.group(1))
        return data['graph']['endpoints'], data['code']

    def test_all_routes(self):
        endpoints, code = self.run_gen()
        routes = {(e['method'], e['path']) for e in endpoints}
        self.assertEqual(len(endpoints), 2, routes)
        self.assertTrue(any(m.upper() == 'GET' for m, _ in routes), routes)
        self.assertTrue(any(m.upper() == 'POST' for m, _ in routes), routes)
        names = {c['name'] for c in code.values()}
        for fn in ('get_items', 'post_item', 'list_items', 'create_item', 'normalize'):
            self.assertIn(fn, names)

    def test_changed(self):
        self.git('checkout', '-q', '-b', 'feature')
        write(os.path.join(self.repo, 'app', 'service.py'), SERVICE_CHANGED)
        self.git('commit', '-q', '-am', 'validate names')
        endpoints, _ = self.run_gen('--changed')
        # only the POST route reaches create_item; GET is untouched
        self.assertEqual([e['method'].upper() for e in endpoints], ['POST'], endpoints)
        changed = [n['title'] for e in endpoints for n in e['nodes'] if n.get('changed')]
        self.assertTrue(any('create_item' in t for t in changed), changed)


if __name__ == '__main__':
    unittest.main()
