import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from src.meditrace.config import Settings


@pytest.mark.parametrize("raw", [None, "", "   ", "\t\n"])
@pytest.mark.parametrize("vercel, expected", [("1", 4000000), ("0", 20 * 1024 * 1024)])
def test_blank_upload_limit_uses_platform_default(monkeypatch, raw, vercel, expected):
    monkeypatch.setenv("VERCEL", vercel)
    if raw is None:
        monkeypatch.delenv("MAX_UPLOAD_BYTES", raising=False)
    else:
        monkeypatch.setenv("MAX_UPLOAD_BYTES", raw)
    assert Settings.from_env().max_upload_bytes == expected


def test_explicit_upload_limit(monkeypatch):
    monkeypatch.setenv("MAX_UPLOAD_BYTES", " 123456 ")
    assert Settings.from_env().max_upload_bytes == 123456


@pytest.mark.parametrize("raw", ["0", "-1", "four million", "4,000,000"])
def test_invalid_upload_limit_has_actionable_error(monkeypatch, raw):
    monkeypatch.setenv("MAX_UPLOAD_BYTES", raw)
    with pytest.raises(ValueError, match="MAX_UPLOAD_BYTES must be"):
        Settings.from_env()


@pytest.mark.parametrize("raw", ["", "   ", "3308"])
def test_blank_mysql_port_uses_default(monkeypatch, raw):
    for name in ["DATABASE_URL", "MYSQL_URL", "POSTGRES_URL"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("MYSQL_HOST", "db.example.test")
    monkeypatch.setenv("MYSQL_PORT", raw)
    assert make_url(Settings.from_env().database_url).port == (
        3308 if raw == "3308" else 3306
    )


@pytest.mark.parametrize("raw", ["0", "-1", "65536", "abc"])
def test_invalid_mysql_port(monkeypatch, raw):
    for name in ["DATABASE_URL", "MYSQL_URL", "POSTGRES_URL"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("MYSQL_HOST", "db.example.test")
    monkeypatch.setenv("MYSQL_PORT", raw)
    with pytest.raises(ValueError, match="MYSQL_PORT must be"):
        Settings.from_env()


def test_vercel_cold_start_with_empty_upload_limit(tmp_path):
    env = dict(
        os.environ,
        VERCEL="1",
        MAX_UPLOAD_BYTES="",
        DATABASE_URL=f'sqlite:///{tmp_path / "test.db"}',
        OBJECT_STORE_BACKEND="database",
    )
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
from api.index import app
from fastapi.testclient import TestClient
with TestClient(app) as client:
    assert client.get('/').status_code == 200
    assert client.get('/api/health').status_code == 200
""",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
