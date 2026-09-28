"""Merkle tree over audit-entry hashes, RFC 6962 style.

* leaf  = SHA-256(0x00 || entry_hash_bytes)
* node  = SHA-256(0x01 || left || right)
* An odd node at the end of a level is promoted unchanged (no duplication,
  which avoids the CVE-2012-2459 duplicate-leaf ambiguity).

Proofs are lists of ``{"side": "L"|"R", "hash": hex}`` from leaf to root.
This module has no project imports so ``scripts/verify_proof.py`` can reuse
the exact same code offline.
"""

from __future__ import annotations

import hashlib


def leaf_hash(entry_hash_hex: str) -> bytes:
    return hashlib.sha256(b"\x00" + bytes.fromhex(entry_hash_hex)).digest()


def node_hash(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def _levels(entry_hashes: list[str]) -> list[list[bytes]]:
    if not entry_hashes:
        raise ValueError("Cannot build a Merkle tree with no leaves")
    level = [leaf_hash(h) for h in entry_hashes]
    levels = [level]
    while len(level) > 1:
        nxt = []
        for i in range(0, len(level), 2):
            if i + 1 < len(level):
                nxt.append(node_hash(level[i], level[i + 1]))
            else:
                nxt.append(level[i])
        level = nxt
        levels.append(level)
    return levels


def merkle_root(entry_hashes: list[str]) -> str:
    return _levels(entry_hashes)[-1][0].hex()


def inclusion_proof(entry_hashes: list[str], index: int) -> list[dict]:
    if not 0 <= index < len(entry_hashes):
        raise IndexError("leaf index out of range")
    proof: list[dict] = []
    for level in _levels(entry_hashes)[:-1]:
        sibling = index ^ 1
        if sibling < len(level):
            proof.append(
                {"side": "L" if sibling < index else "R", "hash": level[sibling].hex()}
            )
        index //= 2
    return proof


def verify_inclusion(entry_hash_hex: str, proof: list[dict], root_hex: str) -> bool:
    try:
        current = leaf_hash(entry_hash_hex)
        for step in proof:
            sibling = bytes.fromhex(step["hash"])
            if step["side"] == "L":
                current = node_hash(sibling, current)
            elif step["side"] == "R":
                current = node_hash(current, sibling)
            else:
                return False
        return current.hex() == root_hex
    except (ValueError, KeyError, TypeError):
        return False
