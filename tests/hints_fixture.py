"""A small FastAPI app shaped like a chat service's file listing, for the review hints on code lines:
a query inside a loop, a keyword the pydantic model doesn't have, and a defaulted field left out.

    python3 tests/hints_fixture.py /tmp/hints && flowsegul --repo /tmp/hints
"""
import os, subprocess, sys, textwrap

FILES = {
    'src/main.py': '''
        from fastapi import FastAPI
        from src.api import chats

        app = FastAPI()
        app.include_router(chats.router)
    ''',
    'src/models/chat.py': '''
        from dataclasses import dataclass
        from enum import Enum
        from typing import Optional
        from pydantic import BaseModel, ConfigDict


        class ExtractionStatus(str, Enum):
            READY = "ready"
            PENDING = "pending"


        class ChatFile(BaseModel):
            name: str
            message_id: str
            message_role: Optional[str]
            extraction_status: ExtractionStatus = ExtractionStatus.READY


        class FileRef(BaseModel):
            name: str
            message_id: str
            preview: bool = False
            size: Optional[int] = None


        class ChatSummary(BaseModel):
            title: str
            count: int
            pinned: bool = False


        class StrictFile(BaseModel):
            model_config = ConfigDict(extra="forbid")
            name: str


        class Upload(BaseModel):
            name: str

            def __init__(self, **data):
                super().__init__(**data)


        @dataclass
        class Notice:
            text: str
            level: str = "info"
    ''',
    'src/persistence/models/file.py': '''
        from sqlalchemy import Column, Integer, String
        from src.persistence.models.base import Base


        class DbFile(Base):
            __tablename__ = "files"
            id = Column(Integer, primary_key=True)
            chat_id = Column(String)
            name = Column(String)
            message_id = Column(String)
            size = Column(Integer)
            extraction_status = Column(String)


        class DbMessage(Base):
            __tablename__ = "messages"
            id = Column(String, primary_key=True)
            role = Column(String)
    ''',
    'src/persistence/models/base.py': '''
        from sqlalchemy.orm import declarative_base

        Base = declarative_base()
    ''',
    'src/persistence/repositories/files.py': '''
        from typing import Optional
        from src.persistence.models.file import DbFile, DbMessage


        class FilesRepository:
            async def get_files_by_chat_id(self, chat_id: str) -> list[DbFile]:
                return []

            async def count_by_kind(self, kind: str) -> int:
                return 0


        class MessageRepository:
            async def get_message_by_id(self, message_id: str) -> Optional[DbMessage]:
                return None
    ''',
    'src/persistence/db_access.py': '''
        from src.persistence.repositories.files import FilesRepository, MessageRepository

        files = FilesRepository()
        message = MessageRepository()
    ''',
    'src/services/chat_service.py': '''
        from src.models.chat import ChatFile
        from src.persistence import db_access


        class ChatService:
            async def get_files(self, chat_id: str) -> list[ChatFile]:
                rows = await db_access.files.get_files_by_chat_id(chat_id=chat_id)
                return [
                    ChatFile(
                        name=row.name,
                        message_id=row.message_id,
                        message_role=None,
                        extraction_status=row.extraction_status,
                    )
                    for row in rows
                ]


        chat_service = ChatService()
    ''',
    'src/services/onedrive/onedrive_actions.py': '''
        from src.models.chat import ChatFile, ChatSummary, FileRef, Notice, StrictFile, Upload
        from src.persistence import db_access


        async def _chat_files_named(chat_id: str, wanted: set[str]) -> list[ChatFile]:
            files = []
            for row in await db_access.files.get_files_by_chat_id(chat_id=chat_id):
                if row.name not in wanted:
                    continue
                message = await db_access.message.get_message_by_id(message_id=row.message_id)
                files.append(
                    ChatFile(
                        name=row.name,
                        message_id=row.message_id,
                        message_role=message.role if message is not None else None,
                        size=row.size,
                    )
                )
            return files


        async def file_refs(chat_id: str) -> list[FileRef]:
            rows = await db_access.files.get_files_by_chat_id(chat_id=chat_id)
            return [FileRef(name=row.name, message_id=row.message_id) for row in rows]


        async def summary(chat_id: str, title: str) -> ChatSummary:
            total = 0
            for kind in ("pdf", "docx"):
                total += await db_access.files.count_by_kind(kind)
            return ChatSummary(title=title, count=total)


        def chat_file_for(name: str, message_id: str) -> ChatFile:
            return ChatFile(name=name, message_id=message_id, message_role=None)


        def odd_calls(name: str, extra: dict) -> list:
            return [
                chat_file_for(name, name),
                StrictFile(name=name, size=1),
                Upload(name=name, size=1),
                ChatFile(name=name, **extra),
                Notice(text=name, urgent=True),
            ]
    ''',
    'src/api/chats.py': '''
        from fastapi import APIRouter
        from src.services.chat_service import chat_service
        from src.services.onedrive import onedrive_actions

        router = APIRouter(prefix="/chats")


        @router.post("/{chat_id}/onedrive")
        async def attach_onedrive_files(chat_id: str, names: list[str]):
            files = await onedrive_actions._chat_files_named(chat_id, set(names))
            refs = await onedrive_actions.file_refs(chat_id)
            return {"files": files, "refs": refs}


        @router.get("/{chat_id}/files")
        async def list_files(chat_id: str):
            return await chat_service.get_files(chat_id)


        @router.get("/{chat_id}/summary")
        async def chat_summary(chat_id: str, title: str):
            notes = onedrive_actions.odd_calls(title, {})
            return await onedrive_actions.summary(chat_id, title)
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
