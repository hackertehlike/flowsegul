"""A tiny FastAPI shop, written out as a git repo with a base commit and a branch commit.

Used by the tests and handy for trying the page by hand:
    python3 tests/shop_fixture.py /tmp/shop && flowsegul --repo /tmp/shop --changed
"""
import os, subprocess, sys, textwrap

COMMON = {
    'src/shop/controllers/orders.py': '''
        from fastapi import APIRouter, HTTPException
        from shop.services.orders import OrderService, preview_price
        from shop.services.carts import CartService

        router = APIRouter(prefix="/orders")


        @router.post("/", status_code=201)
        async def create_order(payload: OrderCreate) -> OrderOut:
            """Place a new order for a customer."""
            service = OrderService()
            order = await service.place_order(payload)
            return order


        @router.get("/{order_id}")
        async def get_order(order_id: int) -> OrderOut:
            """Fetch one order by id."""
            service = OrderService()
            order = await service.get_order(order_id)
            return order


        @router.get("/preview")
        async def preview(total: float, coupon: str) -> float:
            """Price with a coupon, without placing an order."""
            try:
                price = preview_price(total, coupon)
            except ValueError:
                raise HTTPException(status_code=400, detail="unknown coupon")
            return price


        @router.put("/carts/{cart_id}/coupon")
        async def set_cart_coupon(cart_id: int, coupon: str) -> float:
            """Apply a coupon to a cart."""
            carts = CartService()
            total = await carts.set_coupon(cart_id, coupon)
            return total
    ''',
    'src/shop/services/carts.py': '''
        from shop.services.orders import apply_coupon


        class CartService:
            async def set_coupon(self, cart_id: int, coupon: str) -> float:
                subtotal = 100.0
                return apply_coupon(subtotal, coupon)
    ''',
    'src/shop/repositories/orders.py': '''
        class OrderRepository:
            async def insert_order(self, customer_id: int, total: float, items) -> int:
                return 42

            async def fetch_order(self, order_id: int) -> dict:
                return {"id": order_id, "customer_id": 1, "total": 10.0, "items": []}
    ''',
}

SERVICE = '''
    from fastapi import HTTPException
    from shop.repositories.orders import OrderRepository


    def apply_coupon(total: float, coupon) -> float:
    {coupon}


    def compute_total(items) -> float:
        return sum(i.quantity * i.unit_price for i in items)


    def preview_price(total: float, coupon: str) -> float:
        return apply_coupon(total, coupon)


    class OrderService:
        def __init__(self):
            self.orders = OrderRepository()

        async def place_order(self, payload: OrderCreate) -> OrderOut:
            subtotal = compute_total(payload.items)
            total = apply_coupon(subtotal, payload.coupon)
            order_id = await self.orders.insert_order(payload.customer_id, total, payload.items)
            return OrderOut(id=order_id, total=total)

        async def get_order(self, order_id: int) -> OrderOut:
            row = await self.orders.fetch_order(order_id)
            if row is None:
                raise HTTPException(status_code=404, detail="no such order")
            return OrderOut(**row)
'''

COUPON_BASE = '''
        if not coupon:
            return total
        if coupon == "WELCOME10":
            return round(total * 0.9, 2)
        return total'''

COUPON_BRANCH = '''
        if not coupon:
            return total
        rates = {"WELCOME10": 0.9, "VIP20": 0.8}
        if coupon not in rates:
            raise ValueError(f"unknown coupon {coupon}")
        return round(total * rates[coupon], 2)'''


def _write(root, files):
    for rel, body in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(textwrap.dedent(body).lstrip('\n'))


def make(root):
    """Create the repo: `main` has the old coupon rules, `feature` raises on unknown coupons."""
    os.makedirs(root, exist_ok=True)
    git = lambda *a: subprocess.run(['git', '-C', root, *a], check=True, capture_output=True)
    git('init', '-q', '-b', 'main')
    git('config', 'user.email', 't@example.com'); git('config', 'user.name', 't')
    service = lambda body: {'src/shop/services/orders.py': SERVICE.replace(
        '    {coupon}', textwrap.indent(textwrap.dedent(body).strip('\n'), '        '))}
    _write(root, dict(COMMON, **service(COUPON_BASE)))
    git('add', '-A'); git('commit', '-qm', 'base')
    git('checkout', '-qb', 'feature')
    _write(root, service(COUPON_BRANCH))
    git('commit', '-qam', 'raise on unknown coupons')
    return root


if __name__ == '__main__':
    print(make(sys.argv[1]))
