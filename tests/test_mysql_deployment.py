from __future__ import annotations

import importlib
import ssl
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateTable
from sqlalchemy.dialects import mysql
from sqlalchemy.pool import NullPool

from src.meditrace.config import Settings, get_settings
from src.meditrace.models import Base


def test_mysql_environment_handles_special_password(monkeypatch):
    for key in ("DATABASE_URL", "MYSQL_URL", "POSTGRES_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("MYSQL_HOST", "db.example.test")
    monkeypatch.setenv("MYSQL_USER", "meditrace_app")
    monkeypatch.setenv("MYSQL_PASSWORD", "test-only:@/#%")
    monkeypatch.setenv("MYSQL_PORT", "3308")
    settings = Settings.from_env()
    url = make_url(settings.database_url)
    assert url.password == "test-only:@/#%"
    assert url.port == 3308
    assert url.drivername == "mysql+pymysql"
    assert settings.mysql_ssl
    assert settings.object_store_backend == "database"
    assert settings.max_upload_bytes == 4000000


def test_mysql_driver_normalization(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "mysql://app:secret@db.example.test/Meditrace")
    assert Settings.from_env().database_url.startswith("mysql+pymysql://")


def test_mysql_tls_and_serverless_pool(monkeypatch):
    import pymysql
    from src.meditrace.database import build_engine

    captured = {}

    def connect(*args, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("test sentinel")

    monkeypatch.setattr(pymysql, "connect", connect)
    engine = build_engine(
        Settings(
            database_url="mysql+pymysql://app:secret@db.example.test/Meditrace",
            mysql_ssl=True,
            serverless=True,
        )
    )
    assert isinstance(engine.pool, NullPool)
    import pytest

    with pytest.raises(RuntimeError, match="test sentinel"):
        engine.connect()
    assert isinstance(captured["ssl"], ssl.SSLContext)
    assert captured["ssl"].verify_mode == ssl.CERT_REQUIRED
    assert captured["ssl"].check_hostname
    assert captured["connect_timeout"] == 10


def test_mysql_schema_compiles():
    sql = "\n".join(
        str(CreateTable(t).compile(dialect=mysql.dialect()))
        for t in Base.metadata.sorted_tables
    )
    assert "CHAR(32)" in sql
    assert "LONGBLOB" in sql
    assert "FOREIGN KEY" in sql


def setup_app(monkeypatch, url):
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("OBJECT_STORE_BACKEND", "database")
    get_settings.cache_clear()
    import src.meditrace.database as database

    importlib.reload(database)
    import src.meditrace.api as api

    return importlib.reload(api)


def test_persistent_upload_worker_and_failure_health(monkeypatch, tmp_path):
    api = setup_app(monkeypatch, f'sqlite:///{tmp_path / "shared.db"}')
    with TestClient(api.app) as client:
        assert client.get("/api/health").status_code == 200
        content = b"Laboratory report\nHbA1c 7.2 % high"
        upload = client.post(
            "/api/documents",
            data={"patient_id": "SYN-1"},
            files={"file": ("lab.txt", content, "text/plain")},
        )
        assert upload.status_code == 201
        document = upload.json()
        job = client.post(f'/api/documents/{document["id"]}/extract').json()

    # A fresh API instance and separate worker use only the shared database.
    api = setup_app(monkeypatch, f'sqlite:///{tmp_path / "shared.db"}')
    import src.meditrace.worker as worker

    worker = importlib.reload(worker)
    assert worker.process_one()
    with TestClient(api.app) as client:
        assert client.get(f'/api/documents/{document["id"]}/content').content == content
        assert client.get(f'/api/jobs/{job["id"]}').json()["status"] == "completed"
        assert client.get("/api/patients/SYN-1/timeline").json()["total"] > 0

        def fail():
            raise OSError("private connection details")

        monkeypatch.setattr(api.store, "check", fail)
        response = client.get("/api/health")
        assert response.status_code == 503
        assert response.json()["object_store"] == "unavailable"
        assert "private" not in response.text
    get_settings.cache_clear()


def test_vercel_database_failure_does_not_hide_interface(monkeypatch, tmp_path):
    monkeypatch.setenv("VERCEL", "1")
    api = setup_app(monkeypatch, f'sqlite:///{tmp_path / "missing" / "db.sqlite"}')
    with TestClient(api.app) as client:
        assert client.get("/").status_code == 200
        response = client.get("/api/health")
        assert response.status_code == 503
        assert response.json()["database"] == "unavailable"
    get_settings.cache_clear()


def test_live_mysql_upload_worker(monkeypatch):
    """Only run against the disposable CI database (or an explicitly supplied test DB)."""
    import os
    import pytest

    url = os.getenv("MYSQL_TEST_URL")
    if not url:
        pytest.skip("MYSQL_TEST_URL is not set; no live MySQL server available")
    monkeypatch.setenv("MYSQL_SSL", "false")
    api = setup_app(monkeypatch, url)
    with TestClient(api.app) as client:
        assert client.get("/api/health").status_code == 200
        upload = client.post(
            "/api/documents",
            data={"patient_id": "SYN-MYSQL"},
            files={
                "file": (
                    "lab.txt",
                    b"Laboratory report\nHbA1c 7.2 % high",
                    "text/plain",
                )
            },
        )
        assert upload.status_code == 201, upload.text
        doc = upload.json()
        queued = client.post(f'/api/documents/{doc["id"]}/extract')
        assert queued.status_code == 202
        import src.meditrace.worker as worker

        worker = importlib.reload(worker)
        assert worker.process_one()
        assert (
            client.get(f'/api/jobs/{queued.json()["id"]}').json()["status"]
            == "completed"
        )
        assert client.get(f'/api/documents/{doc["id"]}/content').status_code == 200
        assert client.get("/api/patients/SYN-MYSQL/timeline").json()["total"] > 0
    get_settings.cache_clear()
