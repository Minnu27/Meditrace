#!/usr/bin/env python3
"""Verify a MediTrace proof bundle offline, without the server or its database.

    python scripts/verify_proof.py meditrace-proof-fact-1a2b3c4d.json \\
        --public-key <base64 Ed25519 key published by the operator> \\
        [--tsa-ca tsa-cacert.pem [--tsa-cert tsa.crt]]

Deliberately standalone (only needs ``cryptography``) so an auditor can read
all of it. It re-derives every link:

1. commitment  --sha256-->  must equal the audit entry's payload_digest
2. audit entry --sha256-->  must equal entry_hash
3. entry_hash  --Merkle-->  must reach the anchored root
4. anchor statement: root/range match, Ed25519 signature valid
5. optional RFC 3161 token: checked by `openssl ts -verify`

Without --public-key the key embedded in the bundle is used and the result is
reported as self-asserted: pin the operator's published key for a real check.
Exit status is 0 only when every performed check passes.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def canonical_json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def leaf(entry_hash_hex: str) -> bytes:
    return hashlib.sha256(b"\x00" + bytes.fromhex(entry_hash_hex)).digest()


def node(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def verify_inclusion(entry_hash_hex: str, path: list[dict], root_hex: str) -> bool:
    current = leaf(entry_hash_hex)
    for step in path:
        sibling = bytes.fromhex(step["hash"])
        current = node(sibling, current) if step["side"] == "L" else node(current, sibling)
    return current.hex() == root_hex


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--public-key", help="operator's published Ed25519 public key (base64)")
    parser.add_argument("--tsa-ca", type=Path, help="CA certificate(s) for the RFC 3161 TSA")
    parser.add_argument("--tsa-cert", type=Path, help="TSA signing certificate, if not in the token")
    args = parser.parse_args()

    bundle = json.loads(args.bundle.read_text())
    results: list[tuple[str, bool | None, str]] = []

    def check(label: str, ok: bool | None, note: str = "") -> None:
        results.append((label, ok, note))

    entry = bundle.get("audit_entry")
    commitment_hash = sha256_hex(canonical_json(bundle["commitment"]))
    check(f"{bundle['kind']} commitment hash", True, commitment_hash)
    if not entry:
        check("audit entry present", False, "bundle has no audit entry")
    else:
        check("audit entry commits to this record", entry.get("payload_digest") == commitment_hash,
              f"payload_digest {entry.get('payload_digest')}")
        recomputed_entry = sha256_hex(canonical_json(entry))
        check("audit entry hash", recomputed_entry == bundle.get("entry_hash"), f"entry #{entry.get('seq')} -> {recomputed_entry}")

    anchor, proof = bundle.get("anchor"), bundle.get("merkle_proof")
    if not anchor or not proof or not entry:
        check("anchored", None, "not anchored yet; hash checks above are all that can be verified")
    else:
        statement = json.loads(anchor["statement"])
        check("Merkle inclusion", verify_inclusion(bundle["entry_hash"], proof["path"], statement["merkle_root"]),
              f"{len(proof['path'])} steps to root {statement['merkle_root']}")
        in_range = statement["from_seq"] < entry["seq"] <= statement["to_seq"] and proof["leaf_index"] == entry["seq"] - statement["from_seq"] - 1
        check("entry is within the anchored range", in_range, f"seq {statement['from_seq'] + 1}..{statement['to_seq']}")

        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        key_b64 = args.public_key or anchor["public_key"]
        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(key_b64)).verify(
                base64.b64decode(anchor["signature"]), anchor["statement"].encode("utf-8"))
            signed = True
        except (InvalidSignature, ValueError):
            signed = False
        check("anchor signature (Ed25519)", signed,
              "pinned operator key" if args.public_key else "key embedded in bundle (self-asserted: pass --public-key)")
        if args.public_key and anchor["public_key"] != args.public_key:
            check("bundle key matches pinned key", False, "anchor names a different key")

        if anchor.get("tsa_token"):
            openssl = shutil.which("openssl")
            if not args.tsa_ca or not openssl:
                check("RFC 3161 timestamp", None, "token present; pass --tsa-ca (and have openssl) to verify it")
            else:
                with tempfile.TemporaryDirectory() as tmp:
                    data, token = Path(tmp, "statement.json"), Path(tmp, "token.der")
                    data.write_bytes(anchor["statement"].encode("utf-8"))
                    token.write_bytes(base64.b64decode(anchor["tsa_token"]))
                    command = [openssl, "ts", "-verify", "-data", str(data), "-in", str(token), "-token_in", "-CAfile", str(args.tsa_ca)]
                    if args.tsa_cert:
                        command += ["-untrusted", str(args.tsa_cert)]
                    ran = subprocess.run(command, capture_output=True, text=True)
                check("RFC 3161 timestamp (openssl)", "Verification: OK" in ran.stdout, anchor.get("tsa_url") or "")

    width = max(len(label) for label, _, _ in results)
    for label, ok, note in results:
        mark = "PASS" if ok else "----" if ok is None else "FAIL"
        print(f"[{mark}] {label.ljust(width)}  {note}")
    failed = any(ok is False for _, ok, _ in results)
    print("\nRESULT:", "FAILED" if failed else "VERIFIED" if all(ok for _, ok, _ in results) else "PARTIAL (see ---- lines)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
