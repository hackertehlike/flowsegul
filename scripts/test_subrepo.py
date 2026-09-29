"""`--repo` pointing at a subdirectory of the git root. Run: python3 scripts/test_subrepo.py

git speaks two path dialects: `diff --name-only` and `ref:path` are relative to the repository
root, while the indexer keys every file relative to the `--repo` directory. In a monorepo those
differ, `changed_rel_files` matched nothing, and the run died with "No entries found changed".
"""
import os, subprocess, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from flowsegul_gen import Index, changed_rel_files, changed_defs

V1 = 'def handler(x):\n    return x\n'
V2 = 'def handler(x):\n    return x + 1\n\n\ndef added():\n    return 2\n'


def test():
    with tempfile.TemporaryDirectory() as top:
        sub = os.path.join(top, 'backend')          # the repo lives BELOW the git root
        os.makedirs(os.path.join(sub, 'src'))
        f = os.path.join(sub, 'src', 'api.py')
        run = lambda *a: subprocess.run(['git', '-C', top, *a], check=True,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        run('init', '-q')
        run('config', 'user.email', 't@t'); run('config', 'user.name', 't')
        open(f, 'w').write(V1)
        run('add', '-A'); run('commit', '-qm', 'v1')
        base = subprocess.check_output(['git', '-C', top, 'rev-parse', 'HEAD']).decode().strip()
        open(f, 'w').write(V2)
        run('add', '-A'); run('commit', '-qm', 'v2')

        # 1. changed files come back keyed the way the indexer keys them — not 'backend/src/api.py'
        cf = changed_rel_files(sub, base, 'HEAD', merge_base=False)
        assert cf == {'src/api.py'}, cf

        # 2. `git show base:<rel>` resolves, so the old body is found and `handler` reads as
        #    CHANGED rather than new — a broken read returns '' and calls everything new.
        idx = Index(sub, [f])
        quals, models, info, deleted, _ = changed_defs(idx, sub, base, 'HEAD', cf, merge_base=False)
        assert quals == {'src/api.py::handler', 'src/api.py::added'}, quals
        assert info['src/api.py::handler']['new'] is False, info['src/api.py::handler']
        assert info['src/api.py::added']['new'] is True, info['src/api.py::added']

        # 3. reading the whole tree at a ref (--branch) works from the subdirectory too
        assert 'def handler' in Index(sub, [f], ref=base)._read(f)
    print('ok')


if __name__ == '__main__':
    test()
