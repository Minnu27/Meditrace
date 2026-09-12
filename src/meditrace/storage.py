from __future__ import annotations

from pathlib import Path
import os


class LocalObjectStore:
    """Filesystem-backed object store for local work; the API is S3-portable."""

    def __init__(self, root: str):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, key: str, content: bytes) -> None:
        destination = (self.root / key).resolve()
        if self.root not in destination.parents:
            raise ValueError("invalid object key")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        temporary.write_bytes(content)
        os.replace(temporary, destination)

    def get(self, key: str) -> bytes:
        source = (self.root / key).resolve()
        if self.root not in source.parents:
            raise ValueError("invalid object key")
        return source.read_bytes()

    def delete(self, key: str) -> None:
        (self.root / key).unlink(missing_ok=True)

    def check(self) -> None:
        from uuid import uuid4

        key = f".health-{uuid4().hex}"
        try:
            self.put(key, b"ok")
            if self.get(key) != b"ok":
                raise OSError("Storage read-back failed")
        finally:
            self.delete(key)


class DatabaseObjectStore:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    def put(self, key: str, content: bytes) -> None:
        from .models import StoredObject

        with self.session_factory() as session:
            session.add(StoredObject(object_key=key, content=content))
            session.commit()

    def get(self, key: str) -> bytes:
        from .models import StoredObject

        with self.session_factory() as session:
            item = session.get(StoredObject, key)
            if item is None:
                raise FileNotFoundError("Source document not found")
            return item.content

    def delete(self, key: str) -> None:
        from sqlalchemy import delete
        from .models import StoredObject

        with self.session_factory() as session:
            session.execute(delete(StoredObject).where(StoredObject.object_key == key))
            session.commit()

    def check(self) -> None:
        from sqlalchemy import select
        from .models import StoredObject

        with self.session_factory() as session:
            session.execute(select(StoredObject.object_key).limit(1))


def build_object_store(settings):
    if settings.object_store_backend == "database":
        from .database import SessionLocal

        return DatabaseObjectStore(SessionLocal)
    return LocalObjectStore(settings.object_store_path)
