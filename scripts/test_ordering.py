"""Sidebar ordering invariants. Run: python3 scripts/test_ordering.py"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from flowsegul_gen import order_endpoints, ORPHAN_MARK


def ep(group, title, changed):
    return {'group': group, 'title': title, 'nodes': [{'changed': True}] * changed}


def test():
    eps = [ep('r ⟂ database.py', 'DatabaseAccess', 1),
           ep('r/jobs', 'get_job', 3),
           ep('r ⟂ job_service.py', '_work_forever', 9),
           ep('r/chat', 'chat_request', 3),
           ep('r/chat', 'upload', 8),
           ep('r/pii', 'scrub', 1)]
    order_endpoints(eps)
    groups = [e['group'] for e in eps]

    # routes before orphans
    first_orphan = next(i for i, g in enumerate(groups) if ORPHAN_MARK in g)
    assert not any(ORPHAN_MARK in g for g in groups[:first_orphan]), groups
    assert all(ORPHAN_MARK in g for g in groups[first_orphan:]), groups

    # a group's charts stay contiguous — the sidebar prints one header per run
    runs = [g for i, g in enumerate(groups) if i == 0 or groups[i - 1] != g]
    assert len(runs) == len(set(runs)), runs

    # busiest group first, busiest chart first inside it
    assert groups[0] == 'r/chat', groups
    assert eps[0]['title'] == 'upload', eps[0]
    # the 9-change orphan outranks the 1-change one
    assert groups[first_orphan] == 'r ⟂ job_service.py', groups

    # no changes at all (plain run): still contiguous, orphans still last
    plain = [ep('r ⟂ a.py', 'a', 0), ep('r/x', 'x', 0), ep('r ⟂ a.py', 'b', 0), ep('r/x', 'y', 0)]
    order_endpoints(plain)
    assert [e['group'] for e in plain] == ['r/x', 'r/x', 'r ⟂ a.py', 'r ⟂ a.py'], plain
    print('ok')


if __name__ == '__main__':
    test()
