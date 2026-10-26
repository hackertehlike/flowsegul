"""A FastAPI app whose calendar service takes a callback parameter `write` and calls it, while an
unrelated module defines its own `write`. On branch `feat` only that unrelated `write` changes.

    python3 tests/callback_fixture.py /tmp/cb && flowsegul --repo /tmp/cb --changed
"""
import os, subprocess, sys, textwrap

FILES = {
    'app/main.py': '''
        from fastapi import FastAPI
        from app.api import calendar, slides

        app = FastAPI()
        app.include_router(calendar.router, prefix="/calendar")
        app.include_router(slides.router, prefix="/slides")
    ''',
    'app/api/calendar.py': '''
        from fastapi import APIRouter
        from app.services import calendar_service

        router = APIRouter()


        @router.post("/events/{event_id}")
        async def update_event(event_id: int):
            return await calendar_service.runner(lambda i: i)


        @router.delete("/events/{event_id}")
        async def delete_event(event_id: int):
            return await calendar_service._write_to_event(event_id, None, {})
    ''',
    'app/api/slides.py': '''
        from fastapi import APIRouter
        from app.pptx.fill import write as fill_slide
        from app.pptx.shapes import *

        router = APIRouter()


        @router.post("/fill")
        def fill():
            return fill_slide()


        @router.post("/shape")
        def shape():
            return draw()
    ''',
    'app/services/calendar_service.py': '''
        async def runner(write):
            await write(1)


        async def _write_to_event(event_id, write, payload):
            await write(event_id)
            return _audit(event_id)


        def _audit(event_id):
            return event_id


        async def every_kind_of_local(write, *render, draw=None, **fill):
            write(); render(); draw(); fill()
            save: object = None
            save()
            for scale in []:
                scale()
            with open("f") as close:
                close()
            try:
                pass
            except Exception as flush:
                flush()
            if (shape := None):
                shape()
            [stamp() for stamp in []]
            def _audit():
                return 1
            _audit()
            apply = lambda: 1
            apply()
            return (lambda emit: emit())(None)


        def unimported():
            return draw()
    ''',
    'app/pptx/fill.py': '''
        def write():
            return "filled"


        def save():
            return write()
    ''',
    'app/pptx/shapes.py': '''
        def draw():
            return "shape"


        def render():
            return 1


        def scale():
            return 1


        def close():
            return 1


        def flush():
            return 1


        def stamp():
            return 1


        def apply():
            return 1


        def emit():
            return 1


        def shape():
            return 1
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
    git('checkout', '-qb', 'feat')
    with open(os.path.join(root, 'app/pptx/fill.py'), 'w') as f:
        f.write(textwrap.dedent(FILES['app/pptx/fill.py']).lstrip('\n').replace('"filled"', '"filled v2"'))
    git('add', '-A'); git('commit', '-qm', 'feat: change pptx write')
    return root


if __name__ == '__main__':
    print(make(sys.argv[1]))
