"""Bare-name calls resolve the way Python scopes names: the caller's own variables, then a def in
the same module, then what the module imports, and only then a repo-wide match by name, which is
drawn as a maybe and never picks one of several.

Run: python3 -m unittest discover -s tests
"""
import os, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'scripts'))
import flowsegul_gen as gen  # noqa: E402
from test_bugs import page, write  # noqa: E402

FILES = {
    # two modules that each define run() and call their own from another function
    'app/jobs/alpha.py': '''
        def start():
            return run()


        def run():
            return "alpha"
    ''',
    'app/jobs/beta.py': '''
        def start():
            return run()


        def run():
            return "beta"
    ''',
    # same shape as a mail service and a OneDrive service sharing execute_tool
    'app/services/mail.py': '''
        def execute_tool_idempotent(name):
            return execute_tool(name)


        def execute_tool(name):
            return name
    ''',
    'app/services/onedrive.py': '''
        def execute_tool(name):
            return name
    ''',
    'app/jobs/__init__.py': '''
        from .alpha import run as run_alpha
    ''',
    'app/callers.py': '''
        from asyncio import run as aio_run
        from app.jobs.beta import run
        from app.jobs.alpha import run as first
        from app.jobs import run_alpha


        def imported():
            return run()


        def aliased():
            return first()


        def reexported():
            return run_alpha()


        def library(coro):
            return aio_run(coro)


        def param(execute_tool):
            return execute_tool("x")


        def local_import():
            from app.jobs.alpha import run
            return run()


        def comprehension(items):
            names = [run for run in items]
            return run(names)
    ''',
    # a Depends alias names the functions of the file it's written in
    'app/deps.py': '''
        from typing import Annotated
        from fastapi import Depends


        def get_current_user():
            return {"id": 1}


        CurrentUser = Annotated[dict, Depends(get_current_user)]
    ''',
    'app/admin/deps.py': '''
        def get_current_user():
            return {"id": 0, "admin": True}
    ''',
    'app/loose.py': '''
        def by_name_two():
            return run()


        def by_name_one():
            return only_here()


        def builtin(path):
            return open(path)
    ''',
    'app/util.py': '''
        def only_here():
            return 1


        def open(path):
            return path
    ''',
    'app/main.py': '''
        from fastapi import APIRouter
        from app.deps import CurrentUser
        from app.jobs import alpha, beta

        router = APIRouter()


        @router.get("/alpha")
        def get_alpha():
            return alpha.start()


        @router.get("/beta")
        def get_beta():
            return beta.start()


        @router.get("/me")
        def me(user: CurrentUser):
            return user
    ''',
}


class Scope(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo = os.path.join(cls.tmp.name, 'scoped')
        write(cls.repo, FILES)
        cls.idx = gen.Index(cls.repo, gen.gather_py([cls.repo]))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def steps(self, qual):
        return [(s['_qual'], s.get('guess')) for s in gen.steps_for(self.idx.funcs[qual], self.idx)]

    def test_each_run_caller_stays_in_its_own_module(self):
        self.assertEqual(self.steps('app/jobs/alpha.py::start'), [('app/jobs/alpha.py::run', None)])
        self.assertEqual(self.steps('app/jobs/beta.py::start'), [('app/jobs/beta.py::run', None)])

    def test_same_module_def_wins_over_a_namesake(self):
        self.assertEqual(self.steps('app/services/mail.py::execute_tool_idempotent'),
                         [('app/services/mail.py::execute_tool', None)])

    def test_namesake_in_another_module_is_not_a_caller(self):
        callers = [c['qual'] for c in gen.callers_of(self.idx, 'app/services/onedrive.py::execute_tool')]
        self.assertEqual(callers, [])
        callers = [c['qual'] for c in gen.callers_of(self.idx, 'app/services/mail.py::execute_tool')]
        self.assertEqual(callers, ['app/services/mail.py::execute_tool_idempotent'])

    def test_imports_decide_which_one(self):
        self.assertEqual(self.steps('app/callers.py::imported'), [('app/jobs/beta.py::run', None)])
        self.assertEqual(self.steps('app/callers.py::aliased'), [('app/jobs/alpha.py::run', None)])
        self.assertEqual(self.steps('app/callers.py::reexported'), [('app/jobs/alpha.py::run', None)])

    def test_import_inside_the_function_and_comprehension_names(self):
        self.assertEqual(self.steps('app/callers.py::local_import'), [('app/jobs/alpha.py::run', None)])
        self.assertEqual(self.steps('app/callers.py::comprehension'), [('app/jobs/beta.py::run', None)])

    def test_depends_alias_names_its_own_files_function(self):
        self.assertEqual(self.steps('app/main.py::me'), [('app/deps.py::get_current_user', None)])

    def test_library_and_local_names_are_not_ours(self):
        self.assertEqual(self.steps('app/callers.py::library'), [])
        self.assertEqual(self.steps('app/callers.py::param'), [])
        self.assertEqual(self.steps('app/loose.py::builtin'), [])

    def test_name_only_match_is_a_maybe_and_never_picks_one(self):
        two = self.steps('app/loose.py::by_name_two')
        self.assertEqual(sorted(two), [('app/jobs/alpha.py::run', 2), ('app/jobs/beta.py::run', 2)])
        self.assertIsNone(self.idx.resolve(gen.ast.parse('run()').body[0].value, None,
                                           'app/loose.py::by_name_two'))
        self.assertEqual(self.steps('app/loose.py::by_name_one'), [('app/util.py::only_here', 1)])

    def test_page_draws_each_route_into_its_own_run(self):
        subprocess.run(['git', '-C', self.repo, 'init', '-q'], check=True)
        data, _ = page(self.repo)
        eps = {e['path']: e for e in data['graph']['endpoints']}
        for path, mod in (('/alpha', 'alpha'), ('/beta', 'beta')):
            ids = {n['id']: n['fnKey'] for n in eps[path]['nodes']}
            start = next(n for n in eps[path]['nodes'] if n['title'] == 'start')
            self.assertEqual([ids[s['target']].split('::', 1)[1] for s in start['steps']],
                             [f'app/jobs/{mod}.py::run'])
            self.assertTrue(all(not s.get('guess') for s in start['steps']))


if __name__ == '__main__':
    unittest.main()
