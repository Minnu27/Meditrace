"""Periodic Merkle-root anchors over the audit chain.

Each anchor covers the audit entries written since the previous anchor,
``(from_seq, to_seq]``, and signs a small statement::

    {"v": "mt-anchor-v1", "from_seq": ..., "to_seq": ..., "leaf_count": ...,
     "merkle_root": ..., "prev_root": ..., "head_entry_hash": ...,
     "anchored_at": ..., "key_id": ...}

with an Ed25519 key. When ``ANCHOR_TSA_URL`` is set, the same statement is
also timestamped by an independent RFC 3161 authority (tsa.py).

Only hashes leave the system: no fact, answer, patient ID, or audit field is
in the statement, so anchoring never conflicts with deleting data.

Run periodically::

    python -m src.meditrace.anchoring --loop --interval 600

or set ``ANCHOR_INTERVAL_SECONDS`` and the extraction worker does it.
"""

from __future__ import annotations

import argparse
import base64
import logging
import os
from pathlib import Path
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import audit, merkle, tsa
from .models import Anchor, AuditEvent
from .provenance import canonical_json, canonical_timestamp, sha256_hex, utc_now_seconds

ANCHOR_VERSION = "mt-anchor-v1"
GENESIS_ROOT = "0" * 64
_DEV_ANCHOR_KEY_FILE = Path(".meditrace_anchor_key")
log = logging.getLogger(__name__)


class AnchorError(RuntimeError):
    pass


# ------------------------------------------------------------------- keys


def _load_private_key() -> Ed25519PrivateKey:
    configured = os.getenv("ANCHOR_SIGNING_KEY")
    if configured:
        seed = base64.b64decode(configured)
    elif os.getenv("VERCEL") == "1":
        raise AnchorError(
            "ANCHOR_SIGNING_KEY is not set. Generate one with `python -m "
            "src.meditrace.anchoring --generate-key` and store it as a secret."
        )
    elif _DEV_ANCHOR_KEY_FILE.exists():
        seed = base64.b64decode(_DEV_ANCHOR_KEY_FILE.read_text().strip())
    else:
        seed = os.urandom(32)
        _DEV_ANCHOR_KEY_FILE.write_text(base64.b64encode(seed).decode())
        os.chmod(_DEV_ANCHOR_KEY_FILE, 0o600)
    if len(seed) != 32:
        raise AnchorError("ANCHOR_SIGNING_KEY must be base64 of a 32-byte Ed25519 seed")
    return Ed25519PrivateKey.from_private_bytes(seed)


def _raw_public(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(Encoding.Raw, PublicFormat.Raw)


def key_id_for(public_raw: bytes) -> str:
    return sha256_hex(public_raw)[:16]


def server_public_key() -> dict:
    public_raw = _raw_public(_load_private_key().public_key())
    return {
        "algorithm": "Ed25519",
        "public_key": base64.b64encode(public_raw).decode(),
        "key_id": key_id_for(public_raw),
    }


def verify_statement_signature(statement: str, signature_b64: str, public_key_b64: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64)).verify(
            base64.b64decode(signature_b64), statement.encode("utf-8")
        )
        return True
    except (InvalidSignature, ValueError):
        return False


# ---------------------------------------------------------------- anchors


def latest_anchor(session: Session) -> Anchor | None:
    return session.scalar(select(Anchor).order_by(Anchor.to_seq.desc()).limit(1))


def create_anchor(session: Session, *, tsa_url: str | None = None) -> Anchor | None:
    """Anchor every audit entry since the last anchor. Returns None if there is
    nothing new. Refuses to anchor a chain segment that does not verify."""
    last = latest_anchor(session)
    from_seq = last.to_seq if last else 0
    prev_root = last.merkle_root if last else GENESIS_ROOT
    entries = list(
        session.scalars(
            select(AuditEvent).where(AuditEvent.seq > from_seq).order_by(AuditEvent.seq)
        )
    )
    if not entries:
        return None
    report = audit.verify_chain(session, from_seq=from_seq + 1, to_seq=entries[-1].seq)
    if not report.ok:
        raise AnchorError(f"Refusing to anchor a broken audit chain: {report.problems[:3]}")

    private_key = _load_private_key()
    public_raw = _raw_public(private_key.public_key())
    statement_obj = {
        "v": ANCHOR_VERSION,
        "from_seq": from_seq,
        "to_seq": entries[-1].seq,
        "leaf_count": len(entries),
        "merkle_root": merkle.merkle_root([e.entry_hash for e in entries]),
        "prev_root": prev_root,
        "head_entry_hash": entries[-1].entry_hash,
        "anchored_at": canonical_timestamp(utc_now_seconds()),
        "key_id": key_id_for(public_raw),
    }
    statement = canonical_json(statement_obj).decode("utf-8")
    signature = base64.b64encode(private_key.sign(statement.encode("utf-8"))).decode()

    method, token_b64 = "ed25519", None
    if tsa_url:
        try:
            info = tsa.request_timestamp(tsa_url, statement.encode("utf-8"))
            method, token_b64 = "ed25519+rfc3161", base64.b64encode(info.token_der).decode()
        except Exception as exc:  # anchor still stands on its signature
            log.warning("RFC 3161 timestamp failed (%s); anchor is signature-only", type(exc).__name__)

    anchor = Anchor(
        from_seq=from_seq,
        to_seq=statement_obj["to_seq"],
        leaf_count=statement_obj["leaf_count"],
        merkle_root=statement_obj["merkle_root"],
        prev_root=prev_root,
        anchored_at=statement_obj["anchored_at"],
        statement=statement,
        method=method,
        key_id=statement_obj["key_id"],
        public_key=base64.b64encode(public_raw).decode(),
        signature=signature,
        tsa_url=tsa_url if token_b64 else None,
        tsa_token=token_b64,
    )
    session.add(anchor)
    try:
        session.commit()
    except IntegrityError:  # a concurrent anchorer covered the same range
        session.rollback()
        return None
    return anchor


def anchor_containing(session: Session, seq: int) -> Anchor | None:
    return session.scalar(
        select(Anchor).where(Anchor.from_seq < seq, Anchor.to_seq >= seq).limit(1)
    )


def anchor_checks(session: Session, anchor: Anchor) -> dict:
    """Checks that do not depend on any single entry."""
    import json

    statement = json.loads(anchor.statement)
    entries = list(
        session.scalars(
            select(AuditEvent)
            .where(AuditEvent.seq > anchor.from_seq, AuditEvent.seq <= anchor.to_seq)
            .order_by(AuditEvent.seq)
        )
    )
    recomputed_root = merkle.merkle_root([e.entry_hash for e in entries]) if entries else None
    previous = session.scalar(select(Anchor).where(Anchor.to_seq == anchor.from_seq))
    expected_prev = previous.merkle_root if previous else GENESIS_ROOT
    try:
        server_key = server_public_key()["public_key"]
    except AnchorError:
        server_key = None
    return {
        "signature_valid": verify_statement_signature(
            anchor.statement, anchor.signature, anchor.public_key
        ),
        "statement_matches_record": statement.get("merkle_root") == anchor.merkle_root
        and statement.get("to_seq") == anchor.to_seq
        and statement.get("from_seq") == anchor.from_seq,
        "signed_by_this_server": server_key == anchor.public_key,
        "current_log_matches_root": recomputed_root == anchor.merkle_root
        and len(entries) == anchor.leaf_count,
        "links_to_previous_anchor": anchor.prev_root == expected_prev,
        "recomputed_root": recomputed_root,
        "entries": entries,
    }


def anchor_public(anchor: Anchor) -> dict:
    return {
        "id": anchor.id,
        "from_seq": anchor.from_seq,
        "to_seq": anchor.to_seq,
        "leaf_count": anchor.leaf_count,
        "merkle_root": anchor.merkle_root,
        "prev_root": anchor.prev_root,
        "anchored_at": anchor.anchored_at,
        "method": anchor.method,
        "key_id": anchor.key_id,
        "public_key": anchor.public_key,
        "signature": anchor.signature,
        "statement": anchor.statement,
        "tsa_url": anchor.tsa_url,
        "tsa_token": anchor.tsa_token,
    }


# -------------------------------------------------------------------- CLI


def main() -> None:
    from .config import get_settings
    from .database import SessionLocal, create_schema

    parser = argparse.ArgumentParser(description="Anchor the audit chain's Merkle root")
    parser.add_argument("--loop", action="store_true", help="keep anchoring on an interval")
    parser.add_argument("--interval", type=float, default=600)
    parser.add_argument("--tsa-url", default=None, help="RFC 3161 TSA (overrides ANCHOR_TSA_URL)")
    parser.add_argument("--generate-key", action="store_true", help="print a new ANCHOR_SIGNING_KEY")
    args = parser.parse_args()
    if args.generate_key:
        print(base64.b64encode(os.urandom(32)).decode())
        return
    create_schema()
    tsa_url = args.tsa_url or get_settings().anchor_tsa_url
    while True:
        with SessionLocal() as session:
            anchor = create_anchor(session, tsa_url=tsa_url)
        if anchor:
            print(f"anchored seq {anchor.from_seq + 1}..{anchor.to_seq} root={anchor.merkle_root} ({anchor.method})")
        else:
            print("no new audit entries to anchor")
        if not args.loop:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
