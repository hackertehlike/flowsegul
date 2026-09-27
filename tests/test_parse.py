"""Python 3.14 code (`except A, B:` without brackets) still parses on older Pythons.

Run: python3 -m unittest discover tests
"""
import os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'scripts'))
from flowsegul_gen import parse_py  # noqa: E402

SRC = '''
def get_current_user(token):
    try:
        return decode(token)
    except InvalidTokenError, ValidationError:
        raise HTTPException(status_code=403)
'''

# except* needs Python 3.11+ even with brackets
GROUP_SRC = '''
def group(x):
    try:
        run(x)
    except* KeyError, ValueError:
        pass
'''


class ParseTest(unittest.TestCase):
    def test_bare_except_tuple(self):
        tree = parse_py(SRC)
        fn = tree.body[0]
        self.assertEqual(fn.name, 'get_current_user')
        self.assertEqual(fn.body[0].handlers[0].lineno, 5)   # line numbers unchanged

    @unittest.skipIf(sys.version_info < (3, 11), 'except* needs Python 3.11+')
    def test_bare_except_star_tuple(self):
        tree = parse_py(GROUP_SRC)
        self.assertEqual(tree.body[0].body[0].handlers[0].lineno, 5)

    def test_real_syntax_error_still_raises(self):
        with self.assertRaises(SyntaxError):
            parse_py('def f(:\n    pass\n')


if __name__ == '__main__':
    unittest.main()
