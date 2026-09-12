from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import os

from dotenv import load_dotenv
from sqlalchemy.engine import URL


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///./meditrace.db"
    object_store_path: str = "./data/documents"
    max_upload_bytes: int = 20 * 1024 * 1024
    model_endpoint: str | None = None
    model_api_key: str | None = None
    model_name: str = "medgemma"
    auto_process: bool = False
    object_store_backend: str = "local"
    mysql_ssl: bool = False
    mysql_ssl_ca: str | None = None
    serverless: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        vercel = os.getenv("VERCEL") == "1"
        if not vercel:
            load_dotenv(override=False)
        database_url = (
            os.getenv("DATABASE_URL")
            or os.getenv("MYSQL_URL")
            or os.getenv("POSTGRES_URL")
        )
        if not database_url and os.getenv("MYSQL_HOST"):
            database_url = URL.create(
                "mysql+pymysql",
                username=os.getenv("MYSQL_USER"),
                password=os.getenv("MYSQL_PASSWORD"),
                host=os.environ["MYSQL_HOST"],
                port=int(os.getenv("MYSQL_PORT", "3306")),
                database=os.getenv("MYSQL_DATABASE", "Meditrace"),
                query={"charset": "utf8mb4"},
            ).render_as_string(hide_password=False)
        if database_url and database_url.startswith("mysql://"):
            database_url = database_url.replace("mysql://", "mysql+pymysql://", 1)
        backend = os.getenv("OBJECT_STORE_BACKEND") or (
            "database"
            if vercel and database_url and not database_url.startswith("sqlite")
            else "local"
        )
        if backend not in {"local", "database"}:
            raise ValueError("OBJECT_STORE_BACKEND must be local or database")
        if database_url and database_url.startswith("postgresql://"):
            database_url = database_url.replace(
                "postgresql://", "postgresql+psycopg://", 1
            )
        return cls(
            database_url=database_url
            or ("sqlite:////tmp/meditrace.db" if vercel else cls.database_url),
            object_store_path=os.getenv("OBJECT_STORE_PATH")
            or ("/tmp/meditrace-documents" if vercel else cls.object_store_path),
            max_upload_bytes=int(
                os.getenv(
                    "MAX_UPLOAD_BYTES",
                    "4000000" if vercel else str(cls.max_upload_bytes),
                )
            ),
            object_store_backend=backend,
            mysql_ssl=os.getenv("MYSQL_SSL", "true" if vercel else "false").lower()
            in {"1", "true", "yes"},
            mysql_ssl_ca=os.getenv("MYSQL_SSL_CA") or None,
            serverless=vercel,
            model_endpoint=os.getenv("MODEL_ENDPOINT") or None,
            model_api_key=os.getenv("MODEL_API_KEY") or None,
            model_name=os.getenv("MODEL_NAME", cls.model_name),
            auto_process=os.getenv("AUTO_PROCESS", "false").lower()
            in {"1", "true", "yes"},
        )


@lru_cache
def get_settings() -> Settings:
    return Settings.from_env()
