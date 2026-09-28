"""Tamper-evidence tests: every fact and answer is verifiable end to end, and
each kind of tampering is caught by the check that is supposed to catch it."""

from __future__ import annotations

import base64
from datetime import date
import importlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import text

os.environ.setdefault("ENCRYPTION_KEY", "kX8f1QhZ2sYbYQxvV3v4qz5rN0jz3sVfE9pJcRZmA0g=")
os.environ["ANCHOR_SIGNING_KEY"] = base64.b64encode(b"\x07" * 32).decode()

from src.meditrace import merkle  # noqa: E402
from tests.test_meditrace_api import auth_headers, build_client  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
LAB = b"""Riverside Laboratory (synthetic)
Collected: 2026-01-10
HbA1c 8.1 % H (4.0-5.6)
Creatinine 1.0 mg/dL (0.6-1.3)
"""
RX = b"""Prescription (synthetic)
Date prescribed: 2026-01-12
Rx: Metformin 500 mg tablet
Sig: 1 tab PO BID. Dispense #60, Refills: 3
Rx: Lisinopril 10 mg tablet daily
"""


# ------------------------------------------------------------------ helpers


def _worker():
    import src.meditrace.worker as worker

    return importlib.reload(worker)


def _upload(client, headers, patient, name, content, media="text/plain"):
    response = client.post(
        "/api/documents",
        data={"patient_id": patient},
        files={"file": (name, content, media)},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _extract_all(client, headers, documents):
    worker = _worker()
    for document in documents:
        assert client.post(f"/api/documents/{document['id']}/extract", headers=headers).status_code == 202
        assert worker.process_one()


def _facts(client, headers, patient):
    timeline = client.get(f"/api/patients/{patient}/timeline", headers=headers).json()
    return [entry for group in timeline["groups"].values() for entry in group]


def _session():
    from src.meditrace.database import SessionLocal

    return SessionLocal()


def _check(report, check_id):
    return next(c for c in report["checks"] if c["id"] == check_id)


@pytest.fixture
def seeded(tmp_path):
    with build_client(tmp_path) as client:
        headers = auth_headers(client, role="admin")
        lab = _upload(client, headers, "SYN-V", "lab.txt", LAB)
        rx = _upload(client, headers, "SYN-V", "rx.txt", RX)
        _extract_all(client, headers, [lab, rx])
        yield client, headers, {"lab": lab, "rx": rx}


# ------------------------------------------------------------------ merkle


@pytest.mark.parametrize("n", range(1, 12))
def test_every_leaf_has_a_valid_inclusion_proof(n):
    leaves = [uuid.uuid4().hex * 2 for _ in range(n)]
    root = merkle.merkle_root(leaves)
    for index, leaf in enumerate(leaves):
        proof = merkle.inclusion_proof(leaves, index)
        assert merkle.verify_inclusion(leaf, proof, root)
        assert not merkle.verify_inclusion("ab" * 32, proof, root)


def test_merkle_root_changes_with_any_leaf_or_order():
    leaves = [f"{i:064x}" for i in range(5)]
    root = merkle.merkle_root(leaves)
    assert merkle.merkle_root(leaves[:2] + [f"{9:064x}"] + leaves[3:]) != root
    assert merkle.merkle_root(list(reversed(leaves))) != root
    assert merkle.merkle_root(leaves + [leaves[-1]]) != root  # no duplicate-leaf collision


# ---------------------------------------------------------- happy path


def test_extracted_facts_carry_provenance_and_real_dates(seeded):
    client, headers, docs = seeded
    facts = _facts(client, headers, "SYN-V")
    by_name = {f["test_or_finding"]: f for f in facts}
    assert by_name["Hemoglobin A1c"]["observed_date"] == "2026-01-10"
    assert by_name["Hemoglobin A1c"]["status"] == "high"
    assert by_name["Metformin"]["details"]["action"] == "prescribed"
    document = client.get(f"/api/documents/{docs['lab']['id']}", headers=headers).json()
    fact = document["facts"][0]
    assert fact["source_sha256"] == docs["lab"]["sha256"]
    assert fact["extractor"] == "meditrace-deterministic"
    assert len(fact["content_hash"]) == 64


def test_fact_verifies_before_and_after_anchoring(seeded):
    client, headers, _ = seeded
    fact_id = _facts(client, headers, "SYN-V")[0]["id"]

    before = client.get(f"/api/verify/facts/{fact_id}", headers=headers).json()
    assert before["verdict"] == "verified_pending_anchor", before["checks"]
    assert _check(before, "anchor")["status"] == "pending"
    assert _check(before, "quote_in_source")["status"] == "pass"

    anchored = client.post("/api/anchors", headers=headers).json()
    assert anchored["anchored"] and anchored["anchor"]["method"] == "ed25519"

    after = client.get(f"/api/verify/facts/{fact_id}", headers=headers).json()
    assert after["verdict"] == "verified", after["checks"]
    assert {c["status"] for c in after["checks"]} == {"pass"}
    assert after["merkle_proof"]["root"] == anchored["anchor"]["merkle_root"]


def test_answers_are_sealed_cited_and_verifiable(seeded):
    client, headers, _ = seeded
    answer = client.post(
        "/api/patients/SYN-V/ask", json={"question": "What was the HbA1c?"}, headers=headers
    ).json()
    assert answer["answer_id"] and answer["cited_fact_ids"]
    assert answer["answerer"] == "meditrace-deterministic-qa"
    client.post("/api/anchors", headers=headers)
    report = client.get(f"/api/verify/answers/{answer['answer_id']}", headers=headers).json()
    assert report["verdict"] == "verified", report["checks"]
    assert report["cited_facts"][0]["quote"]


def test_every_read_write_and_answer_lands_in_one_valid_chain(seeded):
    client, headers, _ = seeded
    client.post("/api/patients/SYN-V/ask", json={"question": "creatinine?"}, headers=headers)
    client.get("/api/patients/SYN-V/flags", headers=headers)
    client.get("/api/patients/SYN-V/timeline", headers=headers)
    client.get("/api/documents", headers=headers)
    chain = client.get("/api/audit/verify", headers=headers).json()
    assert chain["ok"] and not chain["problems"]
    actions = {e["action"] for e in client.get("/api/audit?limit=1000", headers=headers).json()}
    assert {"upload_document", "extract_fact", "list_documents", "view_timeline",
            "ask_question", "view_flags", "enqueue_extraction"} <= actions
    assert all(e["entry_hash"] for e in client.get("/api/audit", headers=headers).json())


def test_offline_verifier_accepts_a_real_bundle(seeded, tmp_path):
    client, headers, _ = seeded
    fact_id = _facts(client, headers, "SYN-V")[0]["id"]
    client.post("/api/anchors", headers=headers)
    bundle = client.get(f"/api/verify/facts/{fact_id}/bundle", headers=headers)
    assert bundle.status_code == 200
    path = tmp_path / "bundle.json"
    path.write_bytes(bundle.content)
    key = client.get("/api/anchors/public-key").json()["public_key"]
    ok = subprocess.run([sys.executable, "scripts/verify_proof.py", str(path), "--public-key", key],
                        capture_output=True, text=True, cwd=ROOT)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "RESULT: VERIFIED" in ok.stdout

    wrong_key = base64.b64encode(b"\x01" * 32).decode()
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    other = Ed25519PrivateKey.from_private_bytes(b"\x01" * 32).public_key()
    wrong_key = base64.b64encode(other.public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    bad = subprocess.run([sys.executable, "scripts/verify_proof.py", str(path), "--public-key", wrong_key],
                         capture_output=True, text=True, cwd=ROOT)
    assert bad.returncode == 1

    tampered = json.loads(bundle.content)
    tampered["commitment"]["value"] = "5.0"
    path.write_text(json.dumps(tampered))
    bad = subprocess.run([sys.executable, "scripts/verify_proof.py", str(path), "--public-key", key],
                         capture_output=True, text=True, cwd=ROOT)
    assert bad.returncode == 1 and "FAIL" in bad.stdout


def test_reviewer_can_verify_but_not_anchor(seeded):
    client, headers, _ = seeded
    reviewer = auth_headers(client, role="reviewer")
    fact_id = _facts(client, headers, "SYN-V")[0]["id"]
    assert client.get(f"/api/verify/facts/{fact_id}", headers=reviewer).status_code == 200
    assert client.post("/api/anchors", headers=reviewer).status_code == 403
    assert client.get("/api/audit/verify", headers=reviewer).status_code == 403


# ---------------------------------------------------------------- tampering


def test_editing_a_fact_is_caught_even_if_its_hash_is_restamped(seeded):
    client, headers, _ = seeded
    fact_id = next(f["id"] for f in _facts(client, headers, "SYN-V") if f["test_or_finding"] == "Hemoglobin A1c")
    client.post("/api/anchors", headers=headers)
    from src.meditrace.models import Fact
    from src.meditrace.provenance import compute_fact_hash

    with _session() as session:  # naive edit: hash no longer matches content
        fact = session.get(Fact, uuid.UUID(fact_id))
        fact.value = "6.1"
        session.commit()
    report = client.get(f"/api/verify/facts/{fact_id}", headers=headers).json()
    assert report["verdict"] == "failed"
    assert _check(report, "fact_hash")["status"] == "fail"

    with _session() as session:  # key-holding attacker re-stamps content_hash
        fact = session.get(Fact, uuid.UUID(fact_id))
        fact.content_hash = compute_fact_hash(fact)
        session.commit()
    report = client.get(f"/api/verify/facts/{fact_id}", headers=headers).json()
    assert _check(report, "fact_hash")["status"] == "pass"
    assert _check(report, "audit_digest")["status"] == "fail"
    assert report["verdict"] == "failed"


def test_rewriting_the_whole_audit_chain_is_caught_by_the_anchor(seeded):
    client, headers, _ = seeded
    fact_id = _facts(client, headers, "SYN-V")[0]["id"]
    client.post("/api/anchors", headers=headers)
    from src.meditrace import audit
    from src.meditrace.models import AuditEvent

    with _session() as session:
        events = list(session.query(AuditEvent).order_by(AuditEvent.seq))
        events[1].detail = "rewritten history"
        prev = events[0].entry_hash
        for event in events[1:]:  # attacker recomputes every later hash
            event.prev_hash = prev
            event.entry_hash = prev = audit.compute_entry_hash(event)
        session.commit()
        assert audit.verify_chain(session).ok  # internally consistent again...

    chain = client.get("/api/audit/verify", headers=headers).json()
    assert chain["ok"] is False and chain["latest_anchor_valid"] is False  # ...but not vs the anchor
    report = client.get(f"/api/verify/facts/{fact_id}", headers=headers).json()
    assert _check(report, "anchor_log_consistent")["status"] == "fail"


def test_naive_audit_edit_or_deletion_breaks_the_chain(seeded):
    client, headers, _ = seeded
    with _session() as session:
        session.execute(text("UPDATE audit_events SET detail='edited' WHERE seq = 3"))
        session.commit()
    problems = client.get("/api/audit/verify", headers=headers).json()["problems"]
    assert {"seq": 3, "problem": "entry content does not match entry_hash"} in problems
    with _session() as session:
        session.execute(text("DELETE FROM audit_events WHERE seq = 5"))
        session.commit()
    problems = client.get("/api/audit/verify", headers=headers).json()["problems"]
    assert any("missing entry" in p["problem"] for p in problems)


def test_altered_source_bytes_fail_verification_and_extraction(seeded):
    client, headers, docs = seeded
    fact_id = next(f["id"] for f in _facts(client, headers, "SYN-V") if f["source_document_id"] == docs["lab"]["id"])
    from src.meditrace.api import store
    from src.meditrace.models import Document

    with _session() as session:
        key = session.get(Document, uuid.UUID(docs["lab"]["id"])).object_key
    store.put(key, LAB.replace(b"8.1", b"6.1"))
    report = client.get(f"/api/verify/facts/{fact_id}", headers=headers).json()
    assert _check(report, "source_hash")["status"] == "fail"
    assert _check(report, "quote_in_source")["status"] == "fail"

    client.post(f"/api/documents/{docs['lab']['id']}/extract", headers=headers)
    assert _worker().process_one()
    document = client.get(f"/api/documents/{docs['lab']['id']}", headers=headers).json()
    assert document["status"] == "failed" and "SHA-256" in document["extraction_error"]


def test_changing_a_cited_fact_fails_the_answer(seeded):
    client, headers, _ = seeded
    answer = client.post("/api/patients/SYN-V/ask", json={"question": "HbA1c"}, headers=headers).json()
    from src.meditrace.models import Fact
    from src.meditrace.provenance import compute_fact_hash

    with _session() as session:
        fact = session.get(Fact, uuid.UUID(answer["cited_fact_ids"][0]))
        fact.value = "5.2"
        fact.content_hash = compute_fact_hash(fact)
        session.commit()
    report = client.get(f"/api/verify/answers/{answer['answer_id']}", headers=headers).json()
    assert report["verdict"] == "failed"
    assert _check(report, "answer_hash")["status"] == "pass"  # the answer itself is intact


# ------------------------------------------------------------------- gaps


def test_gap_flags_for_medication_without_follow_up(seeded):
    client, headers, _ = seeded
    early = client.get("/api/patients/SYN-V/flags?as_of=2026-03-01", headers=headers).json()
    assert early["gaps"] == []  # nothing is due yet
    late = client.get("/api/patients/SYN-V/flags?as_of=2026-12-01", headers=headers).json()
    kinds = {(g["kind"], g["rule_id"]) for g in late["gaps"]}
    # HbA1c was drawn *before* metformin started, so it is not follow-up.
    assert ("missing_follow_up", "metformin-glycemic-followup") in kinds
    assert ("missing_follow_up", "lisinopril-renal-followup") in kinds
    assert ("abnormal_without_repeat", "abnormal-lab-repeat") in kinds
    assert late["rule_versions"]["gaps"]
    assert all(g["fact_ids"] for g in late["gaps"])

    follow_up = _upload(client, headers, "SYN-V", "lab2.txt",
                        b"Laboratory\nCollected: 2026-04-02\nHbA1c 7.4 % H (4.0-5.6)\nCreatinine 1.0 mg/dL\nPotassium 4.4 mmol/L\n")
    _extract_all(client, headers, [follow_up])
    later = client.get("/api/patients/SYN-V/flags?as_of=2026-12-01", headers=headers).json()
    kinds = {g["rule_id"] for g in later["gaps"] if g["kind"] == "missing_follow_up"}
    assert "metformin-glycemic-followup" not in kinds and "lisinopril-renal-followup" not in kinds


def test_discontinued_medication_is_not_a_gap_or_dose_conflict():
    from src.meditrace.analysis import detect_contradictions, detect_gaps
    from src.meditrace.models import Fact

    def med(dose, day, action):
        return Fact(id=uuid.uuid4(), source_document_id=uuid.uuid4(), patient_id="S", fact_type="medication",
                    test_or_finding="Lisinopril", value=dose, observed_date=date(2026, 1, day),
                    evidence_location={"page": 1}, confidence=0.9, details={"action": action})

    facts = [med("10mg", 1, "documented"), med("10mg", 5, "discontinued"), med("20mg", 9, "documented")]
    assert detect_contradictions(facts) == []
    assert detect_gaps([facts[1]], as_of=date(2027, 1, 1)) == []


# ------------------------------------------------------ extraction & OCR


def test_document_dates_prescriptions_and_ranges():
    from src.meditrace.extraction import deterministic_facts, find_document_date
    from src.meditrace.schemas import DocumentType

    assert find_document_date("Report\nDate of service: 03/14/2026")[0] == date(2026, 3, 14)
    assert find_document_date("Seen 14 Jan 2026 in clinic")[0] == date(2026, 1, 14)
    assert find_document_date("no dates here") is None
    facts = deterministic_facts("Collected: 2026-02-01\nCreatinine 1.6 mg/dL (0.6-1.3)", "S", DocumentType.lab_report)
    assert len(facts) == 1  # the date line is not mistaken for a lab result
    assert facts[0]["status"] == "high" and facts[0]["details"]["status_source"] == "reference_range"
    fallback = deterministic_facts("HbA1c 7.0 %", "S", DocumentType.lab_report, fallback_date=date(2025, 5, 5))
    assert fallback[0]["observed_date"] == date(2025, 5, 5)
    assert fallback[0]["details"]["date_source"] == "upload_date_fallback"


@pytest.mark.skipif(not shutil.which("tesseract"), reason="Tesseract not installed")
def test_scanned_lab_image_is_ocrd_with_confidence(tmp_path):
    pytest.importorskip("pytesseract")
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("L", (1000, 260), 255)
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 30)
    except OSError:
        font = ImageFont.load_default(size=30)
    for i, line in enumerate(["Laboratory report (synthetic)", "Collected: 2026-06-20", "HbA1c 8.4 % H (4.0-5.6)"]):
        draw.text((40, 30 + i * 60), line, fill=0, font=font)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")

    with build_client(tmp_path) as client:
        headers = auth_headers(client)
        document = _upload(client, headers, "SYN-OCR", "scan.png", buffer.getvalue(), "image/png")
        _extract_all(client, headers, [document])
        facts = _facts(client, headers, "SYN-OCR")
        hba1c = next(f for f in facts if f["test_or_finding"] == "Hemoglobin A1c")
        assert hba1c["value"] == "8.4" and hba1c["observed_date"] == "2026-06-20"
        assert hba1c["details"]["ocr"]["engine"].startswith("tesseract")
        report = client.get(f"/api/verify/facts/{hba1c['id']}", headers=headers).json()
        assert _check(report, "quote_in_source")["status"] == "skipped"
        assert report["verdict"] == "verified_pending_anchor"


# ------------------------------------------------------- schema upgrade


def test_legacy_rows_are_chained_and_sealed(tmp_path):
    with build_client(tmp_path) as client:
        headers = auth_headers(client, role="admin")
        document = _upload(client, headers, "SYN-L", "lab.txt", LAB)
        from src.meditrace.database import SessionLocal, engine
        from src.meditrace.models import AuditEvent, Fact
        from src.meditrace.schema_upgrade import upgrade

        with SessionLocal() as session:  # rows as an old release wrote them
            fact = Fact(id=uuid.uuid4(), source_document_id=uuid.UUID(document["id"]), patient_id="SYN-L",
                        fact_type="lab", test_or_finding="HbA1c", value="8.1", unit="%",
                        observed_date=date(2026, 1, 10), evidence_location={"page": 1, "quote": "HbA1c 8.1 %"},
                        confidence=0.9, details={})
            session.add_all([fact, AuditEvent(action="legacy_read", resource_type="patient")])
            session.commit()
        result = upgrade(engine, SessionLocal)
        assert result == {"audit_rows_chained": 1, "facts_sealed": 1}
        assert upgrade(engine, SessionLocal) == {"audit_rows_chained": 0, "facts_sealed": 0}
        assert client.get("/api/audit/verify", headers=headers).json()["ok"]
        report = client.get(f"/api/verify/facts/{fact.id}", headers=headers).json()
        assert _check(report, "legacy")["status"] == "pending"
        assert _check(report, "fact_hash")["status"] == "pass"


def test_upgrade_adds_columns_to_a_pre_chain_database(tmp_path):
    from sqlalchemy import create_engine, inspect
    from sqlalchemy.orm import sessionmaker

    from src.meditrace.models import Base
    from src.meditrace.schema_upgrade import upgrade

    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as connection:  # the audit/fact tables as released before this change
        connection.execute(text("CREATE TABLE audit_events (id CHAR(32) PRIMARY KEY, occurred_at DATETIME, user_email VARCHAR(255), role VARCHAR(32), action VARCHAR(64), resource_type VARCHAR(64), resource_id VARCHAR(64), patient_id VARCHAR(128), success BOOLEAN, detail VARCHAR(500))"))
        connection.execute(text("INSERT INTO audit_events VALUES ('0123456789abcdef0123456789abcdef', '2026-01-01 10:00:00.123456', 'a@x', 'admin', 'login', 'user', NULL, NULL, 1, NULL)"))
    Base.metadata.create_all(engine)  # creates the other tables; leaves audit_events alone
    upgrade(engine, sessionmaker(bind=engine, expire_on_commit=False))
    columns = {c["name"] for c in inspect(engine).get_columns("audit_events")}
    assert {"seq", "prev_hash", "entry_hash", "payload_digest"} <= columns
    assert "uq_audit_events_seq" in {i["name"] for i in inspect(engine).get_indexes("audit_events")}
    with engine.connect() as connection:
        assert connection.execute(text("SELECT seq FROM audit_events")).scalar() == 1


def test_upgrade_ddl_compiles_for_mysql_and_postgres():
    from sqlalchemy.dialects import mysql, postgresql

    from src.meditrace.models import AuditEvent, Fact

    for dialect in (mysql.dialect(), postgresql.dialect()):
        for table in (Fact.__table__, AuditEvent.__table__):
            for column in table.columns:
                assert column.type.compile(dialect=dialect)


# ------------------------------------------------------------ RFC 3161


@pytest.fixture
def local_tsa(tmp_path):
    """A throwaway OpenSSL TSA behind a local HTTP endpoint."""
    if not shutil.which("openssl"):
        pytest.skip("openssl not installed")
    import http.server
    import threading

    d = tmp_path / "tsa"
    d.mkdir()
    (d / "tsa.cnf").write_text(
        "[ tsa ]\ndefault_tsa = t\n[ t ]\ndir = .\nserial = ./serial\ncrypto_device = builtin\n"
        "signer_cert = ./tsa.crt\ncerts = ./tsa.crt\nsigner_key = ./tsa.key\nsigner_digest = sha256\n"
        "default_policy = 1.2.3.4.1\nother_policies = 1.2.3.4.1\ndigests = sha256\naccuracy = secs:1\n"
        "ordering = yes\ntsa_name = no\ness_cert_id_chain = no\ness_cert_id_alg = sha256\n"
        "[ req ]\ndistinguished_name = dn\n[ dn ]\n[ ext ]\nextendedKeyUsage = critical,timeStamping\n"
    )
    (d / "serial").write_text("01\n")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "tsa.key",
                    "-out", "tsa.crt", "-days", "2", "-subj", "/CN=Test TSA", "-config", "tsa.cnf",
                    "-extensions", "ext"], cwd=d, check=True, capture_output=True)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            query = self.rfile.read(int(self.headers["Content-Length"]))
            (d / "q.tsq").write_bytes(query)
            subprocess.run(["openssl", "ts", "-reply", "-config", "tsa.cnf", "-queryfile", "q.tsq",
                            "-out", "r.tsr"], cwd=d, check=True, capture_output=True)
            body = (d / "r.tsr").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/timestamp-reply")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/tsr", d / "tsa.crt"
    server.shutdown()


def test_anchor_with_rfc3161_timestamp_verifies_offline(tmp_path, local_tsa):
    url, ca = local_tsa
    with build_client(tmp_path) as client:
        headers = auth_headers(client, role="admin")
        document = _upload(client, headers, "SYN-T", "lab.txt", LAB)
        _extract_all(client, headers, [document])
        fact_id = _facts(client, headers, "SYN-T")[0]["id"]
        from src.meditrace.anchoring import create_anchor

        with _session() as session:
            anchor = create_anchor(session, tsa_url=url)
        assert anchor.method == "ed25519+rfc3161" and anchor.tsa_token
        report = client.get(f"/api/verify/facts/{fact_id}", headers=headers).json()
        assert _check(report, "anchor_timestamp")["status"] == "pass"

        path = tmp_path / "bundle.json"
        path.write_bytes(client.get(f"/api/verify/facts/{fact_id}/bundle", headers=headers).content)
        key = client.get("/api/anchors/public-key").json()["public_key"]
        result = subprocess.run([sys.executable, "scripts/verify_proof.py", str(path), "--public-key", key,
                                 "--tsa-ca", str(ca)], capture_output=True, text=True, cwd=ROOT)
        assert result.returncode == 0, result.stdout
        assert "[PASS] RFC 3161 timestamp (openssl)" in result.stdout
        assert "RESULT: VERIFIED" in result.stdout


def test_unreachable_tsa_still_produces_a_signed_anchor(seeded):
    from src.meditrace.anchoring import create_anchor

    with _session() as session:
        anchor = create_anchor(session, tsa_url="http://127.0.0.1:9/tsr")
    assert anchor.method == "ed25519" and anchor.tsa_token is None
