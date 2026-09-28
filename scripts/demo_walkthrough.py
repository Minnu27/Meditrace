#!/usr/bin/env python3
"""Seed a synthetic patient through the real pipeline and show verification.

    python scripts/demo_walkthrough.py            # seed SYN-DEMO, extract, ask, anchor
    python scripts/demo_walkthrough.py --tamper   # then silently edit a fact and re-verify

Runs in-process against whatever database/object store your environment
configures (SQLite by default), exactly like the API and worker would:
upload -> OCR/NLP extraction -> sealed facts -> flags (trend, contradiction,
gap) -> grounded answer -> hash-chained audit -> signed Merkle anchor.

Everything here is synthetic. Never point this at real patient data.
"""

from __future__ import annotations

import argparse
import io
import os
from pathlib import Path
import secrets
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PATIENT = "SYN-DEMO"
DEMO_ADMIN = "demo-admin@meditrace.local"

LAB_JAN = b"""Riverside Laboratory - SYNTHETIC
Patient: SYN-DEMO
Collected: 2026-01-10
HbA1c 8.1 % H (4.0-5.6)
Creatinine 1.0 mg/dL (0.6-1.3)
Potassium 4.6 mmol/L (3.5-5.1)
"""
RX_JAN = b"""Prescription - SYNTHETIC
Patient: SYN-DEMO
Date prescribed: 2026-01-12
Rx: Metformin 500 mg tablet
Sig: 1 tab PO BID. Dispense #60, Refills: 3
Rx: Lisinopril 10 mg tablet daily
"""
MEDLIST_MAY = b"""Medication list - SYNTHETIC
Patient: SYN-DEMO
Visit date: 2026-05-02
Metformin 1000 mg tablet twice daily
Lisinopril 10 mg daily
"""
SCANNED_JUNE = [
    "Northgate Laboratory (synthetic)",
    "Patient: SYN-DEMO",
    "Collected: 2026-06-20",
    "HbA1c 7.2 % H (4.0-5.6)",
]
CONFLICT_JUNE = b"""Outside laboratory - SYNTHETIC copy
Patient: SYN-DEMO
Collected: 2026-06-20
HbA1c 7.6 % H (4.0-5.6)
"""


def render_scan(lines: list[str]) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("L", (1100, 90 + 64 * len(lines)), 250)
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 32)
    except OSError:
        font = ImageFont.load_default(size=32)
    for i, line in enumerate(lines):
        draw.text((48, 40 + i * 64), line, fill=20, font=font)
    image = image.rotate(0.6, fillcolor=250)  # a slightly crooked "scan"
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tamper", action="store_true", help="edit a fact afterwards and show detection")
    args = parser.parse_args()

    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from src.meditrace import worker
    from src.meditrace.api import app
    from src.meditrace.auth import create_user, login_required
    from src.meditrace.database import SessionLocal, create_schema
    from src.meditrace.models import User

    create_schema()
    headers = {}
    with TestClient(app) as client:
        if login_required():
            password = secrets.token_urlsafe(18)
            with SessionLocal() as session:
                user = session.scalar(select(User).where(User.email == DEMO_ADMIN))
                if user:
                    session.delete(user)
                    session.commit()
                create_user(session, DEMO_ADMIN, password, "admin")
            token = client.post("/api/auth/login", json={"email": DEMO_ADMIN, "password": password}).json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}

        sources = [
            ("riverside-lab-2026-01.txt", LAB_JAN, "text/plain"),
            ("prescription-2026-01.txt", RX_JAN, "text/plain"),
            ("medication-list-2026-05.txt", MEDLIST_MAY, "text/plain"),
            ("outside-lab-copy-2026-06.txt", CONFLICT_JUNE, "text/plain"),
        ]
        try:
            sources.insert(3, ("northgate-lab-scan-2026-06.png", render_scan(SCANNED_JUNE), "image/png"))
        except ImportError:
            print("! Pillow not installed: skipping the scanned-report (OCR) source")

        print(f"1. Uploading {len(sources)} synthetic sources for {PATIENT}")
        for name, content, media in sources:
            doc = client.post("/api/documents", data={"patient_id": PATIENT},
                              files={"file": (name, content, media)}, headers=headers).json()
            client.post(f"/api/documents/{doc['id']}/extract", headers=headers)
            worker.process_one()
            done = client.get(f"/api/documents/{doc['id']}", headers=headers).json()
            note = f"{len(done['facts'])} facts" if done["status"] == "ready" else f"FAILED: {done['extraction_error']}"
            print(f"   {name:34} sha256 {doc['sha256'][:12]}…  {done['document_type']:16} {note}")

        timeline = client.get(f"/api/patients/{PATIENT}/timeline", headers=headers).json()
        facts = [f for group in timeline["groups"].values() for f in group]
        print(f"\n2. Timeline: {timeline['total']} sealed facts")
        for fact in sorted(facts, key=lambda f: f["observed_date"]):
            ocr = " (OCR)" if fact["details"].get("ocr") else ""
            print(f"   {fact['observed_date']}  {fact['test_or_finding']:16} {str(fact['value'] or ''):>7} {fact['unit'] or '':7} {fact['id'][:8]}{ocr}")

        flags = client.get(f"/api/patients/{PATIENT}/flags?as_of=2026-12-01", headers=headers).json()
        print("\n3. Flags (as of 2026-12-01)")
        for t in flags["trends"]:
            print(f"   TREND        {t['test_or_finding']} {t['direction']} {t['from_value']} -> {t['to_value']}")
        for c in flags["contradictions"]:
            print(f"   CONTRADICTS  {c['summary']}")
        for g in flags["gaps"]:
            print(f"   GAP          {g['summary']}")

        answer = client.post(f"/api/patients/{PATIENT}/ask", json={"question": "What was the most recent HbA1c?"},
                             headers=headers).json()
        print(f"\n4. Q: What was the most recent HbA1c?\n   A: {answer['answer']}\n   answer {answer['answer_id']} cites {[i[:8] for i in answer['cited_fact_ids']]}")

        anchored = client.post("/api/anchors", headers=headers).json()
        if anchored.get("anchored"):
            a = anchored["anchor"]
            print(f"\n5. Anchored audit entries #{a['from_seq'] + 1}–#{a['to_seq']}: root {a['merkle_root'][:24]}… ({a['method']}, key {a['key_id']})")

        target = next(f for f in facts if f["test_or_finding"] == "Hemoglobin A1c")
        report = client.get(f"/api/verify/facts/{target['id']}", headers=headers).json()
        print(f"\n6. Verify fact {target['id']}: {report['verdict'].upper()}")
        for check in report["checks"]:
            print(f"   [{check['status']:7}] {check['label']}")

        if args.tamper:
            import uuid

            from src.meditrace.models import Fact
            from src.meditrace.provenance import compute_fact_hash

            with SessionLocal() as session:
                fact = session.get(Fact, uuid.UUID(target["id"]))
                fact.value = "6.4"  # quietly "improve" the result...
                fact.content_hash = compute_fact_hash(fact)  # ...and re-stamp its hash
                session.commit()
            report = client.get(f"/api/verify/facts/{target['id']}", headers=headers).json()
            print(f"\n7. After a DB-level edit of that fact (value -> 6.4, hash re-stamped): {report['verdict'].upper()}")
            for check in report["checks"]:
                if check["status"] == "fail":
                    print(f"   [fail   ] {check['label']}: {check['detail']}")

    print(f"\nOpen the app and go to  #verify/fact/{target['id']}")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("ANCHOR_INTERVAL_SECONDS", "0")
    sys.exit(main())
