"""Argument-to-parameter bindings. Run: python3 scripts/test_bindings.py

`bindings_for(call, callee)` says which caller expression lands in which callee parameter.
"""
import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from flowsegul_gen import bindings_for


def bind(call_src, params):
    callee = {'node': ast.parse(f"def f({', '.join(params)}): pass").body[0]}
    return bindings_for(ast.parse(call_src, mode='eval').body, callee)


class TestBindings(unittest.TestCase):
    def test_positional_and_keyword(self):
        got = bind('f(a, q=term)', ['id', 'q'])
        self.assertEqual([(b['p'], b['text']) for b in got], [('id', 'a'), ('q', 'term')])

    def test_no_arguments(self):
        self.assertEqual(bind('f()', ['a']), [])

    def test_self_is_not_a_parameter(self):
        self.assertEqual([b['p'] for b in bind('o.m(x)', ['self', 'value'])], ['value'])

    def test_overflow_undeclared_and_unpacked_are_dropped(self):
        self.assertEqual([b['p'] for b in bind('f(a, b, c)', ['first'])], ['first'])
        self.assertEqual(bind('f(nope=x)', ['id']), [])
        self.assertEqual(bind('f(**opts)', ['a']), [])

    def test_attribute_argument_traces_to_its_owner(self):
        b = bind('f(username=auth.username)', ['username'])[0]
        self.assertEqual((b['from'], b['path']), ('auth', '.username'))

    def test_computed_argument_lists_what_it_uses(self):
        self.assertEqual(bind('f(a + b)', ['x'])[0]['uses'], ['a', 'b'])


if __name__ == '__main__':
    unittest.main()
