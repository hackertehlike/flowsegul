"""Regression tests for bugs where routes were incomplete, arrows went to the wrong function, or
changes didn't show up. Each test builds a small FastAPI repo in git and reads the page's data.

Run: python3 -m unittest discover tests
"""
import json, os, re, subprocess, sys, tempfile, textwrap, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GEN = os.path.join(HERE, '..', 'scripts', 'flowsegul_gen.py')

BASE = {
    'app/main.py': '''
        from fastapi import FastAPI
        from app.api import orders
        from app.api.users import router as users_router
        from app.config import settings

        app = FastAPI()
        app.include_router(orders.router, prefix="/orders")
        app.include_router(users_router, prefix=settings.API_PREFIX)
    ''',
    'app/config.py': '''
        class Settings:
            API_PREFIX: str = "/api"

        settings = Settings()
    ''',
    'app/deps.py': '''
        from typing import Annotated
        from fastapi import Depends

        def get_current_user():
            return {"id": 1}

        CurrentUser = Annotated[dict, Depends(get_current_user)]
    ''',
    'app/api/orders.py': '''
        from fastapi import APIRouter
        from app.services.orders import OrderService

        router = APIRouter()
        LIMIT = 10


        @router.get("/")
        @router.get("/all")
        def list_orders():
            return OrderService().list_orders(LIMIT)


        @router.get(path="/{order_id}")
        def get_order(order_id: int):
            return OrderService().get_order(order_id)


        @router.api_route("/ping", methods=["POST"])
        def ping():
            return 1
    ''',
    'app/api/users.py': '''
        from fastapi import APIRouter
        from app.deps import CurrentUser
        from app.services.users import UserService

        router = APIRouter(prefix="/users")


        @router.get("/{user_id}")
        def read_user(user_id: int, me: CurrentUser):
            return UserService().get_user(user_id)
    ''',
    'app/services/orders.py': '''
        from app.repositories.orders import OrderRepository


        class OrderService:
            def __init__(self):
                self.repo = OrderRepository()

            def list_orders(self, limit):
                opts = {"limit": limit}
                return self.repo.all(opts.get("limit"))

            def get_order(self, order_id):
                order = self.repo.get(order_id)
                self.repo.save(order)
                return order
    ''',
    'app/services/users.py': '''
        from app.repositories.users import UserRepository


        class UserService:
            def __init__(self):
                self.repo = UserRepository()

            def get_user(self, user_id):
                return self.repo.get(user_id)
    ''',
    'app/services/legacy.py': '''
        def legacy_thing():
            return 1
    ''',
    'app/repositories/base.py': '''
        class BaseRepository:
            def save(self, obj):
                return obj
    ''',
    'app/repositories/orders.py': '''
        from app.repositories.base import BaseRepository


        class OrderRepository(BaseRepository):
            def get(self, order_id):
                return {"id": order_id}

            def all(self, n):
                return [self.get(i) for i in range(n)]

            def old_helper(self):
                return None
    ''',
    'app/repositories/users.py': '''
        from app.repositories.base import BaseRepository
        from app.repositories.sql import queries


        class UserRepository(BaseRepository):
            def get(self, user_id):
                return {"id": user_id}

            def get_user_row(self, user_id):
                return queries.get_user_row(user_id)
    ''',
    'app/repositories/sql.py': '''
        import aiosql

        queries = aiosql.from_path("queries.sql", "asyncpg")
    ''',
}


def write(root, files):
    for rel, body in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(textwrap.dedent(body).lstrip('\n'))


def make_repo(root, sub=''):
    """`main` has BASE; branch `feat` changes a decorator, a constant, a method, and deletes a
    method and a whole file. The repo's Python code lives in `sub` (a monorepo folder) if given."""
    code = os.path.join(root, sub)
    git = lambda *a: subprocess.run(['git', '-C', root, *a], check=True, capture_output=True)
    os.makedirs(code, exist_ok=True)
    git('init', '-q', '-b', 'main')
    git('config', 'user.email', 't@example.com'); git('config', 'user.name', 't')
    write(code, BASE)
    git('add', '-A'); git('commit', '-qm', 'base')
    git('checkout', '-qb', 'feat')
    edit = lambda rel, a, b: write(code, {rel: textwrap.dedent(BASE[rel]).replace(
        textwrap.dedent(a).strip('\n'), textwrap.dedent(b).strip('\n'))})
    edit('app/api/users.py', '@router.get("/{user_id}")', '@router.get("/by-id/{user_id}")')
    edit('app/api/orders.py', 'LIMIT = 10', 'LIMIT = 500')
    write(code, {'app/repositories/orders.py': textwrap.dedent(BASE['app/repositories/orders.py'])
                 .replace('{"id": order_id}', '{"id": order_id, "v": 2}')
                 .replace('\n    def old_helper(self):\n        return None\n', '')})
    write(code, {'app/main.py': textwrap.dedent(BASE['app/main.py']) + 'app.title = "Shop"\n'})
    os.remove(os.path.join(code, 'app/services/legacy.py'))
    git('add', '-A'); git('commit', '-qm', 'feat')
    return code


def page(repo, *args):
    out = os.path.join(os.path.dirname(repo.rstrip('/')), 'out.html')
    r = subprocess.run([sys.executable, GEN, '--repo', repo, '--out', out, *args],
                       check=True, capture_output=True, text=True)
    with open(out) as f:
        html = f.read()
    return json.loads(re.search(r'^const DATA = (.*);$', html, re.M).group(1)), r.stderr


def by_path(data):
    return {e['method'].upper() + ' ' + e['path']: e for e in data['graph']['endpoints']}


def calls(ep, title):
    n = next(n for n in ep['nodes'] if n['title'] == title)
    ids = {m['id']: m['title'] for m in ep['nodes']}
    return [ids.get(s['target'], s['target']) for s in n['steps']]


class Routes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo = make_repo(os.path.join(cls.tmp.name, 'shop'))
        subprocess.run(['git', '-C', cls.repo, 'checkout', '-q', 'main'], check=True)
        cls.data, _ = page(cls.repo)
        cls.eps = by_path(cls.data)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_include_router_prefixes(self):
        # prefix from include_router, from a settings attribute, and the router's own prefix
        self.assertIn('GET /orders/', self.eps)
        self.assertIn('GET /api/users/{user_id}', self.eps)

    def test_path_keyword_and_api_route(self):
        self.assertIn('GET /orders/{order_id}', self.eps)
        self.assertIn('POST /orders/ping', self.eps)

    def test_one_handler_two_routes_get_their_own_ids(self):
        a, b = self.eps['GET /orders/'], self.eps['GET /orders/all']
        self.assertNotEqual(a['id'], b['id'])

    def test_method_calls_go_to_the_right_class(self):
        self.assertEqual(calls(self.eps['GET /api/users/{user_id}'], 'UserService.get_user'),
                         ['UserRepository.get'])
        # `opts.get(...)` is a dict's get, not OrderRepository.get
        self.assertEqual(calls(self.eps['GET /orders/'], 'OrderService.list_orders'),
                         ['OrderRepository.all'])
        # save() is inherited from BaseRepository
        self.assertEqual(calls(self.eps['GET /orders/{order_id}'], 'OrderService.get_order'),
                         ['OrderRepository.get', 'BaseRepository.save'])

    def test_same_named_method_on_another_object_is_not_a_self_call(self):
        eps = by_path(page(self.repo, '--entries', 'get_user_row')[0])
        self.assertEqual(calls(eps['FN get_user_row'], 'UserRepository.get_user_row'), [])

    def test_dependencies_are_part_of_the_flow(self):
        self.assertIn('get_current_user', calls(self.eps['GET /api/users/{user_id}'], 'read_user'))


class Changes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo = make_repo(os.path.join(cls.tmp.name, 'shop'))
        # a new file nobody has `git add`ed yet
        write(cls.repo, {'app/services/brand_new.py': 'def brand_new():\n    return 2\n'})
        cls.data, _ = page(cls.repo, '--changed', '--base', 'main')
        cls.eps = by_path(cls.data)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def changed(self, key, title):
        return next(n for n in self.eps[key]['nodes'] if n['title'] == title)

    def test_decorator_change_marks_the_route(self):
        n = self.changed('GET /api/users/by-id/{user_id}', 'read_user')
        self.assertTrue(n['changed'])
        self.assertIn('+@router.get("/by-id/{user_id}")', n['diff'])

    def test_constant_change_leads_the_reader_diff(self):
        n = self.changed('GET /orders/', 'list_orders')
        self.assertTrue(n['changed'])
        self.assertRegex(n['diff'], r'@@ orders\.py:\d+ @@\n-LIMIT = 10\n\+LIMIT = 500')

    def test_deleted_file_is_shown(self):
        self.assertIn('DEL legacy_thing', self.eps)

    def test_deleted_method_is_shown_when_another_method_changed(self):
        cls = self.eps['CLASS OrderRepository']['nodes'][0]
        self.assertIn('-    def old_helper(self):', cls['diff'])

    def test_untracked_file_is_shown(self):
        self.assertIn('FN brand_new', self.eps)

    def test_module_level_change_nobody_reads_is_shown(self):
        n = self.eps['MOD main.py']['nodes'][0]
        self.assertIn('+app.title = "Shop"', n['diff'])

    def test_review_mark_key_changes_with_the_code(self):
        base, _ = page(self.repo, '--branch', 'main')
        head, _ = page(self.repo, '--branch', 'feat')
        a, b = by_path(base)['GET /orders/'], by_path(head)['GET /orders/']
        self.assertEqual(a['id'], b['id'])
        self.assertNotEqual(a['sig'], b['sig'])


class Setup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_repo_inside_a_tests_folder(self):
        repo = make_repo(os.path.join(self.tmp.name, 'tests', '.work', 'shop'))
        self.assertIn('GET /orders/', by_path(page(repo)[0]))

    def test_repo_in_a_monorepo_folder(self):
        code = make_repo(os.path.join(self.tmp.name, 'mono'), sub='backend')
        eps = by_path(page(code, '--changed', '--base', 'main')[0])
        self.assertIn('GET /api/users/by-id/{user_id}', eps)
        eps = by_path(page(code, '--changed', '--base', 'main', '--branch', 'feat')[0])
        self.assertIn('DEL legacy_thing', eps)

    def test_unparsable_file_is_reported(self):
        repo = make_repo(os.path.join(self.tmp.name, 'shop'))
        write(repo, {'app/services/broken.py': 'def f(:\n'})
        _, err = page(repo)
        self.assertIn('skipped app/services/broken.py', err)

    def test_launcher_writes_and_names_the_page(self):
        repo = make_repo(os.path.join(self.tmp.name, 'shop'))
        r = subprocess.run(['bash', os.path.join(HERE, '..', 'scripts', 'flowsegul'), '--repo', repo],
                           capture_output=True, text=True, env=dict(os.environ, PATH='/usr/bin:/bin'))
        self.assertEqual(r.returncode, 0, r.stderr)
        path = re.search(r'flowsegul → (\S+)', r.stdout).group(1)
        self.assertTrue(os.path.getsize(path) > 0)


if __name__ == '__main__':
    unittest.main()
