"""A FastAPI chat app that reaches its tools through a registry of stored functions, and hands
work to background tasks and threads. Written out as a git repo: a base commit, then a branch
commit that changes the OneDrive tool and the upload task.

    python3 tests/dispatch_fixture.py /tmp/dx && flowsegul --repo /tmp/dx --changed --base main
"""
import os, subprocess, sys, textwrap

BASE = {
    'app/rest/chat_controller.py': '''
        import asyncio
        from fastapi import APIRouter, BackgroundTasks, UploadFile
        from app.services import chat_service, export_service

        router = APIRouter(prefix="/chat")


        @router.post("")
        async def chat(message: str, username: str):
            return await chat_service.answer(message, username)


        @router.post("/stream")
        async def chat_stream(message: str, username: str):
            return await chat_service.answer(message, username, stream=True)


        @router.post("/files")
        async def upload_local_context_files(files: list[UploadFile], username: str,
                                             background_tasks: BackgroundTasks):
            uploads = [await f.read() for f in files]
            background_tasks.add_task(chat_service.store_uploaded_files, uploads=uploads, username=username)
            return {"queued": len(uploads)}


        @router.post("/summary")
        async def summarize(chat_id: int):
            asyncio.create_task(chat_service.refresh_title(chat_id))
            pdf = await asyncio.to_thread(export_service.render_pdf, chat_id)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, export_service.archive, chat_id)
            return pdf


        @router.get("/export")
        async def export(chat_id: int, fmt: str):
            return export_service.export_rows(chat_id, fmt)
    ''',
    'app/services/chat_service.py': '''
        from app.services import mcp_service, storage, job_service


        async def answer(message: str, username: str, stream: bool = False) -> str:
            tool_name = "onedrive_search" if "file" in message else "mail_send"
            return await mcp_service.call_tool(tool_name, {"q": message}, username)


        async def store_uploaded_files(uploads: list[bytes], username: str) -> None:
            for data in uploads:
                storage.save(username, data)


        async def refresh_title(chat_id: int) -> None:
            print("title", chat_id)
    ''',
    'app/services/mcp_service.py': '''
        from app.services.tools import local_tool_registry


        async def call_tool(tool_name: str, arguments: dict, username: str) -> str:
            local_family = local_tool_registry.family_for_tool(tool_name)
            if local_family is not None:
                tool_output, tool_display = await local_family.execute_tool(
                    tool_name=tool_name, arguments=arguments, username=username)
                return tool_output
            return await remote_tool(tool_name, arguments)


        async def remote_tool(tool_name: str, arguments: dict) -> str:
            return ""
    ''',
    'app/services/tools/local_tool_registry.py': '''
        from typing import Callable, Protocol
        from pydantic import BaseModel
        from app.services import mail_service, onedrive_service


        class ExecuteTool(Protocol):
            async def __call__(self, *, tool_name: str, arguments: dict, username: str) -> tuple[str, str]: ...


        class LocalToolFamily(BaseModel):
            name: str
            is_tool: Callable[[str], bool]
            execute_tool: ExecuteTool


        _MAIL_FAMILY = LocalToolFamily(name="mail", is_tool=mail_service.is_mail_tool,
                                       execute_tool=mail_service.execute_tool_idempotent)
        _ONEDRIVE_FAMILY = LocalToolFamily(name="onedrive", is_tool=lambda n: n.startswith("onedrive_"),
                                           execute_tool=onedrive_service.execute_tool)
        _FAMILIES = (_MAIL_FAMILY, _ONEDRIVE_FAMILY)


        def family_for_tool(tool_name: str) -> LocalToolFamily | None:
            for family in _FAMILIES:
                if family.is_tool(tool_name):
                    return family
            return None
    ''',
    'app/services/mail_service.py': '''
        def is_mail_tool(name: str) -> bool:
            return name.startswith("mail_")


        async def execute_tool_idempotent(*, tool_name: str, arguments: dict, username: str) -> tuple[str, str]:
            return await _send_draft(arguments, username), "sent"


        async def _send_draft(arguments: dict, username: str) -> str:
            return "ok"
    ''',
    'app/services/onedrive_service.py': '''
        async def execute_tool(*, tool_name: str, arguments: dict, username: str) -> tuple[str, str]:
            if tool_name == "onedrive_search":
                return await _search(arguments["q"], username), "search"
            if tool_name == "onedrive_browse":
                return await _browse(arguments.get("path", "/"), username), "browse"
            raise ValueError(tool_name)


        async def _search(q: str, username: str) -> str:
            return q


        async def _browse(path: str, username: str) -> str:
            return path
    ''',
    'app/services/export_service.py': '''
        from dataclasses import dataclass
        from typing import Callable


        def _to_csv(rows: list) -> str:
            return ",".join(map(str, rows))


        def _to_xlsx(rows: list) -> bytes:
            return b""


        @dataclass
        class Exporter:
            fmt: str
            run: Callable[[list], object]


        EXPORTERS = [Exporter("csv", _to_csv), Exporter("xlsx", _to_xlsx)]


        def register_exporter(fmt: str, run: Callable[[list], object]) -> None:
            EXPORTERS.append(Exporter(fmt, run))   # a plugin can add its own: not known until it runs


        def export_rows(chat_id: int, fmt: str):
            rows = [chat_id]
            for exporter in EXPORTERS:
                if exporter.fmt == fmt:
                    return exporter.run(rows)
            return None


        def render_pdf(chat_id: int) -> bytes:
            return b""


        def archive(chat_id: int) -> None:
            print("archive", chat_id)
    ''',
    'app/services/storage.py': '''
        def save(username: str, data: bytes) -> None:
            print(username, len(data))
    ''',
    'app/services/job_service.py': '''
        def enqueue(kind: str, username: str) -> None:
            print(kind, username)
    ''',
}

BRANCH = {
    'app/services/onedrive_service.py': '''
        async def execute_tool(*, tool_name: str, arguments: dict, username: str) -> tuple[str, str]:
            if tool_name == "onedrive_search":
                return await _search(arguments["q"], username), "search"
            if tool_name == "onedrive_browse":
                return await _browse(arguments.get("path", "/"), username), "browse"
            if tool_name == "onedrive_show":
                return await _show(arguments["id"], username), "show"
            raise ValueError(tool_name)


        async def _search(q: str, username: str) -> str:
            return q.strip()


        async def _browse(path: str, username: str) -> str:
            return path


        async def _show(file_id: str, username: str) -> str:
            return file_id
    ''',
    'app/services/chat_service.py': BASE['app/services/chat_service.py'].replace(
        '                storage.save(username, data)\n',
        '                storage.save(username, data)\n'
        '            job_service.enqueue("index_uploads", username)\n'),
}


def write(root, files):
    for rel, body in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(textwrap.dedent(body).lstrip('\n'))


def make(root):
    write(root, BASE)
    git = lambda *a: subprocess.run(['git', '-C', root, *a], check=True, capture_output=True)
    git('init', '-q', '-b', 'main')
    git('config', 'user.email', 't@example.com'); git('config', 'user.name', 't')
    git('add', '-A'); git('commit', '-qm', 'base')
    git('checkout', '-qb', 'feat/onedrive')
    write(root, BRANCH)
    git('add', '-A'); git('commit', '-qm', 'onedrive show')
    return root


if __name__ == '__main__':
    print(make(sys.argv[1]))
