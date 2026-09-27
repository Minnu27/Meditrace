"""Print stored records with encrypted columns decrypted, for operators.

    python -m src.meditrace.show_records                 # every patient
    python -m src.meditrace.show_records --patient SYN-1048

MySQL Workbench shows the sensitive columns (document filenames, fact values,
details and evidence quotes, stored file bytes) as ciphertext on purpose, so a
leaked database dump or a Workbench screenshot does not expose them. This
command reads the same tables through the app's models, which decrypt with
``ENCRYPTION_KEY``. Run it only on a machine you trust; it uses the same
MYSQL_*/DATABASE_URL settings as the API.
"""

from __future__ import annotations

import argparse
import json

from sqlalchemy import select

from .database import SessionLocal
from .models import Document, Fact


def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [
        max(len(h), *(len(r[i]) for r in rows)) if rows else len(h)
        for i, h in enumerate(headers)
    ]
    line = "-+-".join("-" * w for w in widths)
    fmt = lambda cells: " | ".join(c.ljust(w) for c, w in zip(cells, widths))
    return "\n".join([fmt(headers), line, *(fmt(r) for r in rows)])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--patient", help="Only show this patient reference")
    args = parser.parse_args()

    documents = select(Document).order_by(Document.created_at)
    facts = select(Fact).order_by(Fact.patient_id, Fact.observed_date)
    if args.patient:
        documents = documents.where(Document.patient_id == args.patient)
        facts = facts.where(Fact.patient_id == args.patient)

    with SessionLocal() as session:
        doc_rows = [
            [
                d.patient_id,
                d.filename,
                d.media_type,
                d.status.value,
                str(d.size_bytes),
                d.created_at.strftime("%Y-%m-%d %H:%M"),
                str(d.id),
            ]
            for d in session.scalars(documents)
        ]
        fact_rows = [
            [
                f.patient_id,
                str(f.observed_date),
                f.test_or_finding,
                f.value or "",
                f.unit or "",
                f.status or "",
                (f.evidence_location or {}).get("quote") or "",
                json.dumps(f.details) if f.details else "",
            ]
            for f in session.scalars(facts)
        ]

    print(f"DOCUMENTS ({len(doc_rows)})")
    print(
        _table(
            ["patient", "filename", "type", "status", "bytes", "uploaded", "id"],
            doc_rows,
        )
    )
    print(f"\nFACTS ({len(fact_rows)})")
    print(
        _table(
            ["patient", "date", "test/finding", "value", "unit", "status", "quote", "details"],
            fact_rows,
        )
    )


if __name__ == "__main__":
    main()
