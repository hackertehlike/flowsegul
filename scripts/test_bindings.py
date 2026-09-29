"""Argument-to-parameter bindings. Run: python3 scripts/test_bindings.py

The generator's only job in the data-flow feature is `bindings_for`: which caller expression
lands in which callee parameter. Everything built on top of that — following a value across
cards, the lineage panel — lives in the viewer, against the chart actually on screen, and is
covered by scripts/test_viewer.mjs.
"""
import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from flowsegul_gen import SKIP_PARAMS, bindings_for

from hypothesis import given, settings
import hypothesis.strategies as st


@st.composite
def call_and_params(draw):
    """A random ast.Call plus the callee parameter list the indexer would have produced.

    The pool the callee declares from deliberately includes SKIP_PARAMS names, so the test can
    prove they never survive — the indexer strips them before bindings_for ever sees the list.
    """
    names = ['user_id', 'tenant', 'payload', 'limit', 'offset']
    n_pos = draw(st.integers(min_value=0, max_value=5))
    kw_names = draw(st.lists(st.sampled_from(names), min_size=0, max_size=3, unique=True))
    n_star = draw(st.integers(min_value=0, max_value=1))

    call = ast.Call(
        func=ast.Name(id='callee', ctx=ast.Load()),
        args=[ast.Name(id=f'a{i}', ctx=ast.Load()) for i in range(n_pos)],
        keywords=([ast.keyword(arg=k, value=ast.Name(id=f'v_{k}', ctx=ast.Load())) for k in kw_names]
                  + [ast.keyword(arg=None, value=ast.Name(id='extra', ctx=ast.Load()))] * n_star),
    )
    declared = draw(st.lists(st.sampled_from(names + sorted(SKIP_PARAMS)),
                             min_size=0, max_size=6, unique=True))
    return call, [p for p in declared if p not in SKIP_PARAMS], n_pos, len(kw_names), n_star


class TestProperty1BindingCompleteness(unittest.TestCase):

    @given(call_and_params())
    @settings(max_examples=200)
    def test_one_entry_per_argument_and_no_skip_params(self, case):
        """
        # Feature: data-flow-visualization, Property 1: binding list completeness and SKIP_PARAMS exclusion

        For any call, bindings has exactly one entry per positional argument plus one per keyword
        argument (star-unpacks included), overflow positionals bind to '*args', and no SKIP_PARAMS
        name ever appears as a param_name.

        Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6
        """
        call, params, n_pos, n_kw, n_star = case
        bindings = bindings_for(call, params)

        # 1.1/1.5 — every argument is represented, none dropped
        self.assertEqual(len(bindings), n_pos + n_kw + n_star, bindings)

        # 1.1 — positional bindings keep call-site order
        self.assertEqual([b['arg_expr'] for b in bindings[:n_pos]], [f'a{i}' for i in range(n_pos)])

        # 1.2/1.4 — positionals resolve by position, overflow goes to *args
        for k in range(n_pos):
            self.assertEqual(bindings[k]['param_name'], params[k] if k < len(params) else '*args')

        # 1.3/1.4 — a keyword binds to its own name, or '?' when undeclared; **d binds to **kwargs
        for b in bindings[n_pos:]:
            if b['arg_expr'].startswith('**'):
                self.assertEqual(b['param_name'], '**kwargs')
            else:
                self.assertTrue(b['param_name'] in params or b['param_name'] == '?', b)

        # 1.6 — SKIP_PARAMS are stripped upstream and must never resurface
        for b in bindings:
            self.assertNotIn(b['param_name'], SKIP_PARAMS, b)


class TestBindingsExamples(unittest.TestCase):
    """The cases the random strategy only reaches by luck."""

    @staticmethod
    def _call(src):
        return ast.parse(src, mode='eval').body

    def test_zero_argument_call_has_no_bindings(self):
        self.assertEqual(bindings_for(self._call('f()'), ['a', 'b']), [])

    def test_keyword_only_call_uses_keyword_names(self):
        self.assertEqual(bindings_for(self._call('f(id=user_id, q=term)'), ['id', 'q']),
                         [{'arg_expr': 'user_id', 'param_name': 'id'},
                          {'arg_expr': 'term', 'param_name': 'q'}])

    def test_undeclared_keyword_is_unresolved(self):
        self.assertEqual(bindings_for(self._call('f(nope=x)'), ['id']),
                         [{'arg_expr': 'x', 'param_name': '?'}])

    def test_overflow_positionals_bind_to_star_args(self):
        got = bindings_for(self._call('f(a, b, c)'), ['first'])
        self.assertEqual([b['param_name'] for b in got], ['first', '*args', '*args'])

    def test_double_star_unpack(self):
        self.assertEqual(bindings_for(self._call('f(**opts)'), ['a']),
                         [{'arg_expr': '**opts', 'param_name': '**kwargs'}])

    def test_skip_params_never_appear(self):
        # the indexer hands over an already-filtered list, so the first real argument binds to
        # the first real parameter — `self` is simply not in it
        self.assertEqual(bindings_for(self._call('obj.m(x)'), ['value']),
                         [{'arg_expr': 'x', 'param_name': 'value'}])

    def test_attribute_argument_keeps_its_full_expression(self):
        # the viewer traces `auth_data.username` back to the card that owns `auth_data`
        self.assertEqual(bindings_for(self._call('f(username=auth_data.username)'), ['username']),
                         [{'arg_expr': 'auth_data.username', 'param_name': 'username'}])


if __name__ == '__main__':
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    if not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful():
        sys.exit(1)
    print('\nAll tests passed.')
