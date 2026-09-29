"""Folded code in a box: each call and raise knows its own lines in the box's code, and the
if/else/for/with/try lines around it, so the box can show those and fold the rest.

Run: python3 -m unittest discover tests
"""
import json, os, re, subprocess, sys, tempfile, textwrap, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GEN = os.path.join(HERE, '..', 'scripts', 'flowsegul_gen.py')

FILES = {
    'app/main.py': '''
        from fastapi import FastAPI
        from app.api import router

        app = FastAPI()
        app.include_router(router)
    ''',
    'app/deps.py': '''
        from typing import Annotated
        from fastapi import Depends

        def get_db():
            return object()

        def check_admin():
            return True

        Db = Annotated[object, Depends(get_db)]
    ''',
    'app/api.py': '''
        from fastapi import APIRouter, Depends, HTTPException
        from app.deps import Db, check_admin
        from app.work import load, save, notify, audit, cleanup

        router = APIRouter()


        @router.post(
            "/jobs",
            dependencies=[Depends(check_admin)],
        )
        def run_job(db: Db, name: str):
            job = load(db, name)
            if job is None:
                raise HTTPException(status_code=404, detail="no job")
            elif job == "old":
                audit(name)
            else:
                for step in job:
                    save(
                        db,
                        step,
                    )
            try:
                notify(name)
            except ValueError:
                audit(name)
            finally:
                cleanup(db)
            return job
    ''',
    'app/work.py': '''
        def load(db, name):
            return [name]

        def save(db, step):
            return step

        def notify(name):
            return name

        def audit(name):
            return name

        def cleanup(db):
            return db
    ''',
}


class FoldedCode(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        repo = os.path.join(cls.tmp.name, 'jobs')
        for rel, src in FILES.items():
            p = os.path.join(repo, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, 'w') as f:
                f.write(textwrap.dedent(src).lstrip())
        out = repo + '.html'
        subprocess.run([sys.executable, GEN, '--repo', repo, '--out', out, '--no-open'],
                       check=True, capture_output=True)
        with open(out) as f:
            data = json.loads(re.search(r'^const DATA = (.*);$', f.read(), re.M).group(1))
        ep = data['graph']['endpoints'][0]
        cls.node = next(n for n in ep['nodes'] if n['title'] == 'run_job')
        cls.lines = data['code'][cls.node['fnKey']]['code'].split('\n')

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def text(self, span):
        return [self.lines[i].strip() for i in range(span[0], span[1] + 1)]

    def step(self, needle):
        return next(s for s in self.node['steps'] if needle in (s.get('expr') or ''))

    def test_call_knows_its_own_lines(self):
        self.assertEqual(self.text(self.step('load(').get('ln')), ['job = load(db, name)'])
        self.assertEqual(self.text(self.step('save(')['ln']), ['save(', 'db,', 'step,', ')'])

    def test_blocks_around_a_call(self):
        heads = [self.text(c)[0] for c in self.step('save(')['ctx']]
        self.assertEqual(heads, ['if job is None:', 'elif job == "old":', 'else:', 'for step in job:'])
        audit = [s for s in self.node['steps'] if s.get('expr') == 'audit(name)']
        self.assertEqual([self.text(c)[0] for c in audit[0]['ctx']], ['if job is None:', 'elif job == "old":'])
        self.assertEqual([self.text(c)[0] for c in audit[1]['ctx']], ['try:', 'except ValueError:'])
        self.assertEqual([self.text(c)[0] for c in self.step('cleanup(')['ctx']], ['try:', 'finally:'])

    def test_raise_knows_its_lines(self):
        r = self.node['raises'][0]
        self.assertEqual(self.text(r['ln']), ['raise HTTPException(status_code=404, detail="no job")'])
        self.assertEqual([self.text(c)[0] for c in r['ctx']], ['if job is None:'])

    def test_dependencies_point_at_the_signature_and_decorator(self):
        deps = {s['expr']: s for s in self.node['steps'] if s.get('dep')}
        admin = next(s for e, s in deps.items() if 'check_admin' in e)
        self.assertEqual(self.text(admin['ln']), ['dependencies=[Depends(check_admin)],'])
        self.assertEqual([self.text(c)[0] for c in admin['ctx']], ['@router.post('])
        db = next(s for e, s in deps.items() if 'get_db' in e)   # through the `Db` alias
        self.assertEqual(self.text(db['ln']), ['def run_job(db: Db, name: str):'])


if __name__ == '__main__':
    unittest.main()
