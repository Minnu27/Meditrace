from __future__ import annotations

import importlib
import os

from fastapi.testclient import TestClient


def build_client(tmp_path):
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp_path / 'test.db'}"
    os.environ["OBJECT_STORE_PATH"] = str(tmp_path / "objects")
    os.environ["ENCRYPTION_KEY"] = "kX8f1QhZ2sYbYQxvV3v4qz5rN0jz3sVfE9pJcRZmA0g="
    os.environ["SECRET_KEY"] = "test-secret-key-at-least-32-bytes-long!!"
    from src.meditrace import config

    config.get_settings.cache_clear()
    import src.meditrace.database as database
    import src.meditrace.api as api

    importlib.reload(database)
    api = importlib.reload(api)
    return TestClient(api.app)


def auth_headers(client: TestClient, role: str = "clinician") -> dict[str, str]:
    """Bootstraps a user directly (no HTTP registration endpoint by design) and logs in."""
    from src.meditrace.auth import create_user
    from src.meditrace.database import SessionLocal, create_schema

    create_schema()
    email = f"{role}@example.test"
    with SessionLocal() as session:
        create_user(session, email, "correct horse battery staple", role)
    response = client.post(
        "/api/auth/login", json={"email": email, "password": "correct horse battery staple"}
    )
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_unauthenticated_requests_are_rejected(tmp_path):
    with build_client(tmp_path) as client:
        assert client.get("/api/documents").status_code == 401
        assert (
            client.post("/api/documents", data={"patient_id": "SYN-1"}).status_code == 401
        )


def test_upload_list_download_and_evidence_fact(tmp_path):
    with build_client(tmp_path) as client:
        headers = auth_headers(client)
        upload = client.post(
            "/api/documents",
            data={"patient_id": "SYN-1048"},
            files={"file": ("lab.txt", b"HbA1c 7.2 %", "text/plain")},
            headers=headers,
        )
        assert upload.status_code == 201
        document = upload.json()
        assert document["sha256"]
        assert document["status"] == "uploaded"

        listing = client.get("/api/documents", headers=headers).json()
        assert listing["total"] == 1
        assert listing["items"][0]["patient_id"] == "SYN-1048"

        content = client.get(f"/api/documents/{document['id']}/content", headers=headers)
        assert content.content == b"HbA1c 7.2 %"

        fact = client.post(
            f"/api/documents/{document['id']}/facts",
            json={
                "patient_id": "SYN-1048",
                "fact_type": "lab",
                "test_or_finding": "HbA1c",
                "normalized_code": "4548-4",
                "value": "7.2",
                "unit": "%",
                "reference_range": "4.0-5.6",
                "status": "high",
                "observed_date": "2026-08-30",
                "evidence_location": {
                    "page": 1,
                    "line_start": 1,
                    "line_end": 1,
                    "quote": "HbA1c 7.2 %",
                },
                "confidence": 0.98,
            },
            headers=headers,
        )
        assert fact.status_code == 201
        assert fact.json()["source_document_id"] == document["id"]

        # A reviewer can read but not write.
        reviewer_headers = auth_headers(client, role="reviewer")
        assert (
            client.get(f"/api/documents/{document['id']}", headers=reviewer_headers).status_code
            == 200
        )
        assert (
            client.post(
                f"/api/documents/{document['id']}/facts",
                json={
                    "patient_id": "SYN-1048",
                    "fact_type": "lab",
                    "test_or_finding": "HbA1c",
                    "value": "7.3",
                    "observed_date": "2026-09-01",
                    "evidence_location": {"page": 1, "quote": "HbA1c 7.3 %"},
                    "confidence": 0.9,
                },
                headers=reviewer_headers,
            ).status_code
            == 403
        )


def test_rejects_unsupported_media_type(tmp_path):
    with build_client(tmp_path) as client:
        headers = auth_headers(client)
        rejected = client.post(
            "/api/documents",
            data={"patient_id": "SYN-1"},
            files={"file": ("bad.exe", b"x", "application/x-msdownload")},
            headers=headers,
        )
        assert rejected.status_code == 415


def test_extraction_queue_and_deterministic_timeline(tmp_path):
    with build_client(tmp_path) as client:
        headers = auth_headers(client)
        document = client.post(
            "/api/documents",
            data={"patient_id": "SYN-TIME"},
            files={
                "file": (
                    "lab.txt",
                    b"Laboratory report\nHbA1c 7.2 % high",
                    "text/plain",
                )
            },
            headers=headers,
        ).json()
        queued = client.post(f"/api/documents/{document['id']}/extract", headers=headers)
        assert queued.status_code == 202
        assert queued.json()["status"] == "queued"
        assert (
            client.get(f"/api/jobs/{queued.json()['id']}", headers=headers).status_code == 200
        )

        for observed_date, value in (("2026-07-01", "7.0"), ("2026-08-01", "7.5")):
            result = client.post(
                f"/api/documents/{document['id']}/facts",
                json={
                    "patient_id": "SYN-TIME",
                    "fact_type": "lab",
                    "test_or_finding": "HbA1c",
                    "value": value,
                    "unit": "%",
                    "observed_date": observed_date,
                    "evidence_location": {"page": 1, "line_start": 2, "quote": "HbA1c"},
                    "confidence": 0.9,
                },
                headers=headers,
            )
            assert result.status_code == 201
        timeline = client.get("/api/patients/SYN-TIME/timeline", headers=headers).json()
        assert timeline["total"] == 2
        assert timeline["groups"]["2026-08"][0]["numeric_delta"] == 0.5
        assert timeline["groups"]["2026-08"][0]["source_document_id"] == document["id"]
        assert timeline["groups"]["2026-08"][0]["reliability_tier"] in {"high", "medium", "low"}

        flags = client.get("/api/patients/SYN-TIME/flags", headers=headers).json()
        assert flags["trends"][0]["direction"] == "rising"

        answer = client.post(
            "/api/patients/SYN-TIME/ask",
            json={"question": "What was the HbA1c result?"},
            headers=headers,
        ).json()
        assert answer["cited_fact_ids"]
        assert not answer["insufficient_evidence"]
