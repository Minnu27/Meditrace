from __future__ import annotations

from collections.abc import Generator
import ssl

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from .config import Settings, get_settings
from .models import Base


def build_engine(settings: Settings):
    kwargs = (
        {"connect_args": {"check_same_thread": False}}
        if settings.database_url.startswith("sqlite")
        else {}
    )
    if settings.database_url.startswith("mysql"):
        connect_args = {"connect_timeout": 10, "read_timeout": 30, "write_timeout": 30}
        if settings.mysql_ssl or settings.mysql_ssl_ca:
            connect_args["ssl"] = ssl.create_default_context(
                cafile=settings.mysql_ssl_ca
            )
        kwargs.update(connect_args=connect_args, pool_recycle=280)
    if settings.serverless:
        kwargs["poolclass"] = NullPool
    return create_engine(settings.database_url, pool_pre_ping=True, **kwargs)


settings = get_settings()
engine = build_engine(settings)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def create_schema() -> None:
    Base.metadata.create_all(engine)


def get_session() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session
