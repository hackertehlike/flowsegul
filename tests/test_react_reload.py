"""React mode: a mutation that refreshes a list leads on to the list that reloads.

`invalidateQueries({ queryKey })` after a delete makes every query whose key matches load again,
so the click's path goes on to the component reading that query and the request it sends.
Skips without node + typescript. Run with: python3 -m unittest discover -s tests -v
"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_react import SKIP, ANALYZER  # noqa: E402

FILES = {
    'package.json': '{"name": "notes", "dependencies": {"react": "18", "@tanstack/react-query": "5"}}',
    'tsconfig.json': '{"compilerOptions": {"jsx": "react-jsx", "strict": true}}',
    'src/api/client.ts': '''
        import axios from "axios";
        export const api = axios.create({ baseURL: import.meta.env.VITE_API });
    ''',
    'src/api/comments.ts': '''
        import { infiniteQueryOptions, useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
        import { api } from "./client";

        export const getComments = (postId: string) => api.get(`/comments?post=${postId}`);
        export const commentsOptions = (postId: string) =>
          infiniteQueryOptions({ queryKey: ["comments", postId], queryFn: () => getComments(postId), initialPageParam: 1, getNextPageParam: () => undefined });
        export const useComments = (postId: string) => useInfiniteQuery({ ...commentsOptions(postId) });
        export const tagsKey = ["tags"];
        export const useTags = () => useInfiniteQuery({ queryKey: ["tags", 1], queryFn: () => api.get("/tags"), initialPageParam: 1, getNextPageParam: () => undefined });

        export const useDeleteComment = (postId: string) => {
          const qc = useQueryClient();
          return useMutation({
            mutationFn: (id: string) => api.delete(`/comments/${id}`),
            onSuccess: () => {
              qc.invalidateQueries({ queryKey: commentsOptions(postId).queryKey });
              qc.invalidateQueries({ queryKey: tagsKey });
            },
          });
        };
    ''',
    'src/Comments.tsx': '''
        import { useComments, useDeleteComment, useTags } from "./api/comments";

        export function Comments({ postId }: { postId: string }) {
          const comments = useComments(postId);
          const tags = useTags();
          const del = useDeleteComment(postId);
          return <button onClick={() => del.mutate("1")}>Delete</button>;
        }
    ''',
}


@unittest.skipIf(SKIP, SKIP or '')
class ReloadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        for rel, body in FILES.items():
            path = os.path.join(cls.tmp.name, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w') as f:
                f.write(textwrap.dedent(body).lstrip('\n'))
        p = subprocess.run(['node', ANALYZER, cls.tmp.name], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True)
        assert p.returncode == 0, p.stderr
        cls.d = json.loads(p.stdout)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_delete_reloads_the_list(self):
        a = next(a for a in self.d['actions'] if a['label'] == 'click Delete')
        rows = self.d['rows']
        first = [rows[s['r'][0]]['t'].strip() for s in a['steps']]
        apis = [x[:2] for s in a['steps'] for r in s['r'] for x in rows[r].get('api', [])]
        # the invalidate line itself is a step, not only the key inside it
        self.assertIn('qc.invalidateQueries({ queryKey: commentsOptions(postId).queryKey });', first)
        # then the component line reading that query, and the request it sends again
        i = first.index('qc.invalidateQueries({ queryKey: commentsOptions(postId).queryKey });')
        self.assertEqual(first[i + 1], 'const comments = useComments(postId);')
        self.assertIn(['GET', '/comments'], apis)
        # a key held in a const (`tagsKey = ["tags"]`) matches by its first word
        self.assertIn('const tags = useTags();', first)
        self.assertIn(['GET', '/tags'], apis)
        self.assertEqual(apis[0], ['DELETE', '/comments/${id}'])


if __name__ == '__main__':
    unittest.main()
