"""A small full-stack repo: a FastAPI backend and a React frontend that calls it, for linking each
frontend request to the route it reaches.

Used by tests/test_link.py and handy for trying the page by hand:
    python3 tests/fullstack_fixture.py /tmp/fullshop && flowsegul --repo /tmp/fullshop

The frontend talks to the backend through Vite's dev proxy, which strips "/api". It calls in
several ways (fetch, a small `api()` wrapper, an axios instance with a baseURL, a module of named
client functions), and some calls are wrong on purpose:
  - PriceLine asks for /orders/preview, but `/orders/{order_id}` is declared first and catches it;
    it also sends `n` where the route reads `item_count`
  - RefundButton sends GET to a POST route
  - ReceiptLink asks for /order/{id}/receipt, which doesn't exist
  - OrderList asks for /orders without the trailing slash
  - Avatar fetches a URL it gets as a prop, which can't be read before run time
  - PriceLine reads the preview as a number, but the route answers with a PreviewOut object
  - Reorder sends `qty` where OrderIn has `quantity`, and QuickAdd posts no body at all
  - UserCount reads `count` as a number; Stats sends it as a string
  - OrderSummary expects `id` as a string, `note` never null, and `items[].quantity` that isn't there
"""
import os, subprocess, sys, textwrap

FILES = {
    'backend/app/main.py': '''
        from fastapi import FastAPI
        from app.routes import orders, users

        app = FastAPI()
        app.include_router(orders.router)
        app.include_router(users.router, prefix="/users")
    ''',
    'backend/app/routes/orders.py': '''
        from fastapi import APIRouter, Query
        from pydantic import BaseModel

        router = APIRouter(prefix="/orders")


        class OrderIn(BaseModel):
            sku: str
            quantity: int


        class LineOut(BaseModel):
            sku: str
            qty: int


        class OrderOut(BaseModel):
            id: int
            total: float
            note: str | None = None
            items: list[LineOut] = []


        class PreviewOut(BaseModel):
            total: float


        @router.get("/")
        def list_orders(page: int = 1, size: int = Query(20)):
            return []


        @router.post("/")
        def create_order(payload: OrderIn):
            return {"id": 1}


        @router.get("/{order_id}", response_model=OrderOut)
        def get_order(order_id: int):
            return {"id": order_id, "total": 0}


        @router.get("/preview")
        def preview_order(code: str, item_count: int) -> PreviewOut:
            return PreviewOut(total=9.5)


        @router.post("/{order_id}/refund")
        def refund_order(order_id: int):
            return {"ok": True}
    ''',
    'backend/app/routes/users.py': '''
        from fastapi import APIRouter, Request
        from pydantic import BaseModel

        router = APIRouter()


        class Stats(BaseModel):
            count: str


        @router.get("/stats")
        def user_stats() -> Stats:
            return Stats(count="3")


        @router.get("/me")
        def read_me():
            return {"name": "Ada"}


        @router.get("/search")
        def search_users(request: Request, q: str):
            return []


        @router.delete("/{user_id}")
        def delete_user(user_id: int):
            return None
    ''',
    'frontend/package.json': '''
        {"name": "shopweb", "private": true, "dependencies": {"react": "^18.2.0", "axios": "^1.7.0"}}
    ''',
    'frontend/tsconfig.json': '''
        {"compilerOptions": {"jsx": "react-jsx", "strict": true, "module": "esnext",
                             "moduleResolution": "bundler", "target": "es2020"},
         "include": ["src"]}
    ''',
    'frontend/vite.config.ts': '''
        import { defineConfig } from "vite";

        export default defineConfig({
          server: {
            proxy: {
              "/api": { target: "http://localhost:8000", rewrite: (path) => path.replace(/^\\/api/, "") },
            },
          },
        });
    ''',
    'frontend/src/lib/api.ts': '''
        import axios from "axios";

        const BASE = "/api";

        export async function api<T>(path: string, init?: RequestInit): Promise<T> {
          const res = await fetch(BASE + path, init);
          return res.json();
        }

        export const http = axios.create({ baseURL: "/api" });
    ''',
    'frontend/src/lib/users.ts': '''
        import { http } from "./api";

        export const usersApi = {
          me: () => http.get("/users/me"),
          search: (q: string) => http.get("/users/search", { params: { q, limit: 5 } }),
          remove: (id: number) => http.delete(`/users/${id}`),
        };
    ''',
    'frontend/src/App.tsx': '''
        import { PriceLine } from "./checkout/PriceLine";
        import { OrderPanel } from "./orders/OrderPanel";
        import { Avatar, UserCount } from "./users/Avatar";

        export default function App() {
          return (
            <main>
              <PriceLine couponCode="WELCOME10" />
              <OrderPanel />
              <Avatar src="/img/ada.png" />
              <UserCount />
            </main>
          );
        }
    ''',
    'frontend/src/checkout/PriceLine.tsx': '''
        import { useEffect, useState } from "react";

        export function PriceLine({ couponCode }: { couponCode: string }) {
          const [items] = useState<string[]>([]);
          const [total, setTotal] = useState<number>();
          useEffect(() => {
            fetch(`/api/orders/preview?code=${couponCode}&n=${items.length}`)
              .then((r) => r.json())
              .then((t: number) => setTotal(t));
          }, [couponCode, items]);
          return <span>{total}</span>;
        }
    ''',
    'frontend/src/orders/OrderPanel.tsx': '''
        import { useState } from "react";
        import axios from "axios";
        import { api, http } from "../lib/api";
        import { usersApi } from "../lib/users";

        export function OrderPanel() {
          const [order, setOrder] = useState<{ id: number } | null>(null);
          const [orders, setOrders] = useState<unknown[]>([]);
          const open = async (id: number) => setOrder(await api<{ id: number }>(`/orders/${id}`));
          const place = () => api("/orders/", { method: "POST", body: JSON.stringify({ sku: "A1", quantity: 1 }) });
          return (
            <section>
              <button onClick={() => open(1)}>Open order</button>
              <button onClick={place}>Place order</button>
              <button onClick={() => api("/orders/", { method: "POST", body: JSON.stringify({ sku: "A1", qty: 2 }) })}>Reorder</button>
              <button onClick={() => http.post("/orders/")}>Quick add</button>
              <OrderSummary id={order?.id ?? 0} />
              <button onClick={async () => setOrders((await http.get("/orders", { params: { page: 2 } })).data)}>Load orders</button>
              <RefundButton id={order?.id ?? 0} />
              <ReceiptLink id={order?.id ?? 0} />
              <button onClick={() => usersApi.search("ada")}>Find users</button>
              <button onClick={() => axios.get("https://api.github.com/repos/fastapi/fastapi")}>Stars</button>
              <span>{orders.length}</span>
            </section>
          );
        }

        type Order = { id: string; total: number; note: string; items: { sku: string; quantity: number }[] };

        function OrderSummary({ id }: { id: number }) {
          const [o, setO] = useState<Order>();
          return <button onClick={async () => setO(await api<Order>(`/orders/${id}`))}>{o?.note}</button>;
        }

        function RefundButton({ id }: { id: number }) {
          return <button onClick={() => http.get(`/orders/${id}/refund`)}>Refund</button>;
        }

        function ReceiptLink({ id }: { id: number }) {
          return <button onClick={() => api(`/order/${id}/receipt`)}>Receipt</button>;
        }
    ''',
    'frontend/src/users/Avatar.tsx': '''
        import { useEffect, useState } from "react";
        import { usersApi } from "../lib/users";

        export function Avatar({ src }: { src: string }) {
          const [blob, setBlob] = useState<Blob>();
          useEffect(() => {
            fetch(src).then((r) => r.blob()).then(setBlob);
            usersApi.me();
          }, [src]);
          return <img alt="" src={blob ? URL.createObjectURL(blob) : ""} />;
        }

        export function UserCount() {
          const [n, setN] = useState(0);
          const load = async () => {
            const res = await fetch("/api/users/stats");
            const data: { count: number } = await res.json();
            setN(data.count);
          };
          return <button onClick={load}>{n} users</button>;
        }
    ''',
}


def make(root, git=True):
    for rel, body in FILES.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(textwrap.dedent(body).lstrip('\n'))
    if git and not os.path.isdir(os.path.join(root, '.git')):
        run = lambda *a: subprocess.run(['git', '-C', root, *a], check=True, capture_output=True)
        run('init', '-q', '-b', 'main')
        run('config', 'user.email', 't@example.com'); run('config', 'user.name', 't')
        run('add', '-A'); run('commit', '-qm', 'shop')
    return root


if __name__ == '__main__':
    print(make(sys.argv[1]))
