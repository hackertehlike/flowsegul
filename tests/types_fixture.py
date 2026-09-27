"""A small FastAPI app with values handed across functions in the wrong type (and some that are
fine), for the mismatched-types marks.

    python3 tests/types_fixture.py /tmp/typeshop && flowsegul --repo /tmp/typeshop
"""
import os, subprocess, sys, textwrap

FILES = {
    'app/__init__.py': '',
    'app/api/__init__.py': '',
    'app/services/__init__.py': '',
    'app/repositories/__init__.py': '',
    'app/api/customers.py': '''
        import os
        from uuid import UUID
        from fastapi import APIRouter, HTTPException, UploadFile
        from app.schemas import CustomerCreate, CustomerOut
        from app.services.customers import CustomerService, sign, save_note, make_token, notify

        router = APIRouter(prefix="/customers")


        @router.post("/")
        async def create_customer(payload: CustomerCreate) -> CustomerOut:
            service = CustomerService()
            customer = service.load(payload.customer_id)
            return customer


        @router.get("/{customer_id}")
        async def get_customer(customer_id: UUID) -> CustomerOut:
            service = CustomerService()
            customer = service.find(customer_id)
            if customer is None:
                raise HTTPException(status_code=404)
            notify(make_token(customer_id), None)
            return service.describe(customer)


        @router.get("/{customer_id}/email")
        async def customer_email(customer_id: UUID) -> str:
            service = CustomerService()
            return service.email_of(customer_id)


        @router.post("/{customer_id}/notes")
        async def add_note(customer_id: UUID, file: UploadFile) -> str:
            data = await file.read()
            save_note(customer_id, data)
            key = os.getenv("SIGNING_KEY")
            return sign(key, f"note {data}")


        @router.get("/{customer_id}/raw")
        async def raw_customer(customer_id: UUID) -> CustomerOut:
            service = CustomerService()
            return service.find(customer_id)
    ''',
    'app/schemas.py': '''
        from uuid import UUID
        from pydantic import BaseModel


        class CustomerCreate(BaseModel):
            customer_id: str
            nickname: str | None = None


        class CustomerOut(BaseModel):
            id: UUID
            email: str
    ''',
    'app/models.py': '''
        from uuid import UUID


        class Customer:
            id: UUID
            email: str
            name: str
    ''',
    'app/services/customers.py': '''
        from typing import Optional
        from uuid import UUID
        import jwt
        from app.models import Customer
        from app.repositories.customers import CustomerRepository


        class CustomerService:
            def __init__(self):
                self.repo = CustomerRepository()

            def load(self, customer_id: UUID) -> Customer:
                return self.repo.get(customer_id)

            def find(self, customer_id: UUID) -> Optional[Customer]:
                return self.repo.get(customer_id)

            def describe(self, customer: Customer) -> Customer:
                return customer

            def email_of(self, customer_id: UUID) -> str:
                customer = self.repo.get(customer_id)
                if customer:
                    print("found")
                return customer.email


        def save_note(customer_id: UUID, text: str) -> None:
            print(customer_id, text)


        def sign(key: str, text: str) -> str:
            return key + text


        def make_token(customer_id: UUID) -> str:
            token = jwt.encode({"sub": str(customer_id)}, "secret", algorithm="HS256")
            return token


        def notify(text: str, extra: dict = None) -> None:
            print(text, extra)
    ''',
    'app/repositories/customers.py': '''
        from uuid import UUID
        from app.models import Customer


        class CustomerRepository:
            def get(self, customer_id: UUID) -> Customer | None:
                return None
    ''',
}


def make(root):
    for rel, src in FILES.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(textwrap.dedent(src).lstrip())
    run = lambda *a: subprocess.run(['git', *a], cwd=root, check=True, capture_output=True)
    run('init', '-q', '-b', 'main')
    run('add', '.')
    run('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'base')
    return root


if __name__ == '__main__':
    make(sys.argv[1] if len(sys.argv) > 1 else '/tmp/typeshop')
