"""A small FastAPI app whose routes write to the database, commit in different ways and call the
outside world, for the side-effect strip.

    python3 tests/effects_fixture.py /tmp/fx && flowsegul --repo /tmp/fx
"""
import os, subprocess, sys, textwrap

FILES = {
    '.flowsegul.json': '{"irreversible": ["notify_*"]}',
    'app/deps.py': '''
        from typing import Annotated
        from fastapi import Depends
        from sqlalchemy.orm import Session
        from app.db import SessionLocal
        from app.models import User


        def get_db():
            db = SessionLocal()
            try:
                yield db
                db.commit()
            finally:
                db.close()


        def get_plain_session():
            with SessionLocal() as session:
                yield session


        def get_current_user(session: Annotated[Session, Depends(get_plain_session)]) -> User:
            return session.get(User, 1)


        SessionDep = Annotated[Session, Depends(get_db)]
        CurrentUser = Annotated[User, Depends(get_current_user)]
        PlainSession = Annotated[Session, Depends(get_plain_session)]
    ''',
    'app/services/mail.py': '''
        import smtplib


        def send_receipt(to: str, total: float) -> None:
            with smtplib.SMTP("localhost") as smtp:
                smtp.sendmail("shop@example.com", [to], f"total {total}")
    ''',
    'app/services/partners.py': '''
        def notify_partner(order_id: int) -> None:
            print("partner", order_id)
    ''',
    'app/services/pricing.py': '''
        def apply_coupon(total: float, coupon: str) -> float:
            if coupon and coupon != "WELCOME10":
                raise ValueError("unknown coupon")
            return total * 0.9 if coupon else total
    ''',
    'app/tasks.py': '''
        from celery import shared_task


        @shared_task
        def rebuild_report(user_id: int) -> None:
            pass
    ''',
    'app/api/routes.py': '''
        import os
        import httpx
        from fastapi import APIRouter, BackgroundTasks
        from sqlalchemy import update
        from app.deps import SessionDep, PlainSession, CurrentUser
        from app.models import Order, User, Payment, Document
        from app.services.mail import send_receipt
        from app.services.partners import notify_partner
        from app.services.pricing import apply_coupon
        from app.tasks import rebuild_report

        router = APIRouter()


        @router.post("/orders")
        def create_order(email: str, total: float, coupon: str, db: SessionDep):
            order = Order(email=email, total=total)
            db.add(order)
            send_receipt(email, total)
            order.total = apply_coupon(total, coupon)
            notify_partner(order.id)
            return order


        @router.post("/users")
        def create_user(email: str, notify: bool, session: PlainSession):
            user = User(email=email)
            session.add(user)
            session.commit()
            if notify:
                send_receipt(email, 0)
            return user


        @router.post("/payments/{payment_id}/refund")
        def refund(payment_id: int, session: PlainSession, background_tasks: BackgroundTasks):
            with session.begin():
                session.execute(update(Payment).where(Payment.id == payment_id).values(refunded=True))
                httpx.post("https://psp.example.com/refunds", json={"id": payment_id})
            background_tasks.add_task(send_receipt, "a@example.com", 0)
            return {"ok": True}


        @router.delete("/documents/{doc_id}")
        def delete_document(doc_id: int, session: PlainSession):
            doc = session.get(Document, doc_id)
            session.delete(doc)
            os.remove(doc.path)
            return {"ok": True}


        @router.post("/reports/{user_id}")
        def request_report(user_id: int, session: PlainSession):
            session.execute(update(User).where(User.id == user_id).values(report_pending=True))
            rebuild_report.delay(user_id)
            session.commit()
            return {"queued": True}


        @router.patch("/me")
        def update_me(name: str, current_user: CurrentUser, session: PlainSession):
            current_user.name = name
            session.add(current_user)
            session.commit()
            return current_user


        @router.get("/orders/{order_id}")
        def read_order(order_id: int, session: PlainSession):
            return session.get(Order, order_id)
    ''',
}


def make(root):
    for rel, body in FILES.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(textwrap.dedent(body).lstrip('\n'))
    git = lambda *a: subprocess.run(['git', '-C', root, *a], check=True, capture_output=True)
    git('init', '-q', '-b', 'main')
    git('config', 'user.email', 't@example.com'); git('config', 'user.name', 't')
    git('add', '-A'); git('commit', '-qm', 'base')
    return root


if __name__ == '__main__':
    print(make(sys.argv[1]))
