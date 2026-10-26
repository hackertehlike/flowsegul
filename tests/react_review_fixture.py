"""A small React + TypeScript app in two commits, for the "state erased" mark of a diff.

Used by tests/test_react_review.py and handy for trying the page by hand:
    python3 tests/react_review_fixture.py /tmp/ordersweb
    flowsegul --repo /tmp/ordersweb --react --changed --base HEAD~1

First commit ("base"): an orders page. Second commit ("head", the PR): the coupon field moves
into a promo panel that only shows while `showPromo` is on, so its typed draft is now erased when
the panel closes. The rest (a context, a component declared inside another, a random key, a
query without a loading check) is there to show that nothing else gets marked.
"""
import os, subprocess, sys, textwrap

BASE = {
    'package.json': '''
        {"name": "ordersweb", "private": true, "dependencies": {"react": "^18.2.0", "@tanstack/react-query": "^5.0.0"}}
    ''',
    'tsconfig.json': '''
        {"compilerOptions": {"jsx": "react-jsx", "strict": true, "module": "esnext",
                             "moduleResolution": "bundler", "target": "es2020"},
         "include": ["src"]}
    ''',
    'src/App.tsx': '''
        import { OrdersPage } from "./OrdersPage";

        export default function App() {
          return <OrdersPage />;
        }
    ''',
    'src/CartContext.ts': '''
        import { createContext } from "react";

        export const CartContext = createContext<string[]>([]);
    ''',
    'src/useOrders.ts': '''
        import { useQuery } from "@tanstack/react-query";

        export function useOrders(page: number) {
          return useQuery({ queryKey: ["orders", page], queryFn: () => fetch(`/orders?page=${page}`).then((r) => r.json()) });
        }
    ''',
    'src/OrdersPage.tsx': '''
        import { useEffect, useRef, useState } from "react";
        import { useQuery } from "@tanstack/react-query";
        import { CartContext } from "./CartContext";
        import { useOrders } from "./useOrders";
        import { PriceLine } from "./PriceLine";
        import { CouponField } from "./CouponField";
        import { Badge } from "./Badge";

        export function OrdersPage() {
          const [page, setPage] = useState(1);
          const [couponCode, setCouponCode] = useState("");
          const items = ["a", "b"];
          const title = "Orders";
          const seen = useRef(0);
          const { data } = useOrders(page);
          const stats = useQuery({ queryKey: ["stats"], queryFn: () => fetch("/stats").then((r) => r.json()) });
          const { data: tags } = useQuery({ queryKey: ["tags"], queryFn: () => fetch("/tags").then((r) => r.json()) });
          const resetPage = () => setPage(1);
          useEffect(() => {
            document.title = title + " " + page;
            seen.current += 1;
            if (page > 99) resetPage();
          }, [page]);
          if (stats.isLoading) return <p>Loading</p>;
          if (stats.error) return <p>Failed</p>;
          return (
            <CartContext.Provider value={items}>
              <h1>{data.length} orders, {stats.data.total} total</h1>
              <p>{(tags ?? []).join(", ")}</p>
              <PriceLine couponCode={couponCode} />
              <CouponField onApply={setCouponCode} />
              <Badge />
              <button onClick={() => setPage(page + 1)}>Next</button>
            </CartContext.Provider>
          );
        }
    ''',
    'src/PriceLine.tsx': '''
        import { useContext, useEffect, useState } from "react";
        import { CartContext } from "./CartContext";

        export function PriceLine({ couponCode }: { couponCode: string }) {
          const items = useContext(CartContext);
          const [total, setTotal] = useState<number>();
          useEffect(() => {
            fetch(`/orders/preview?code=${couponCode}&n=${items.length}`)
              .then((r) => r.json())
              .then(setTotal);
          }, [couponCode]);
          return <span>{total}</span>;
        }
    ''',
    'src/CouponField.tsx': '''
        import { useState } from "react";

        export function CouponField({ onApply }: { onApply: (code: string) => void }) {
          const [draft, setDraft] = useState("");
          return (
            <div>
              <input value={draft} onChange={(e) => setDraft(e.target.value)} aria-label="coupon" />
              <button onClick={() => onApply(draft)}>Apply</button>
            </div>
          );
        }
    ''',
    'src/Badge.tsx': '''
        import { useContext, useState } from "react";
        import { CartContext } from "./CartContext";

        export function Badge() {
          const items = useContext(CartContext);
          function Counter() {
            const [n, setN] = useState(0);
            return <button onClick={() => setN(n + 1)}>{n}</button>;
          }
          return (
            <span>
              {items.length}
              <Counter />
              <Tip key={Math.random()} />
            </span>
          );
        }

        function Tip() {
          const [open, setOpen] = useState(false);
          return <i onClick={() => setOpen(!open)}>?</i>;
        }
    ''',
}

# the PR: the coupon field moves into a promo panel that only shows while showPromo is on
HEAD = {
    'src/OrdersPage.tsx': BASE['src/OrdersPage.tsx']
        .replace('import { CouponField } from "./CouponField";', 'import { PromoPanel } from "./PromoPanel";')
        .replace('<CouponField onApply={setCouponCode} />', '<PromoPanel onApply={setCouponCode} />'),
    'src/PromoPanel.tsx': '''
        import { useState } from "react";
        import { CouponField } from "./CouponField";

        export function PromoPanel({ onApply }: { onApply: (code: string) => void }) {
          const [showPromo, setShowPromo] = useState(false);
          return (
            <section>
              <button onClick={() => setShowPromo(!showPromo)}>Promo</button>
              {showPromo && <CouponField onApply={onApply} />}
            </section>
          );
        }
    ''',
}


def _write(root, files):
    for rel, body in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(textwrap.dedent(body).lstrip('\n'))


def make(root):
    run = lambda *a: subprocess.run(['git', '-C', root, *a], check=True, capture_output=True)
    _write(root, BASE)
    run('init', '-q', '-b', 'main')
    run('config', 'user.email', 't@example.com'); run('config', 'user.name', 't')
    run('add', '-A'); run('commit', '-qm', 'orders page')
    _write(root, HEAD)
    run('add', '-A'); run('commit', '-qm', 'promo panel')
    return root


if __name__ == '__main__':
    print(make(sys.argv[1]))
