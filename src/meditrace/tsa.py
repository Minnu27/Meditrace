"""Minimal RFC 3161 timestamp-authority client, no extra dependencies.

A server-signed anchor only proves the log hasn't changed since *this server*
signed it; the operator could re-sign a rewritten log. A token from an
independent TSA (e.g. https://freetsa.org/tsr) proves the anchored statement
existed at the TSA's clock time, which the operator cannot backdate.

We build the request in DER by hand, bind the reply to our request (status,
message imprint, nonce), and keep the full token. Checking the TSA's CMS
signature chain is delegated to OpenSSL, which does it properly::

    openssl ts -verify -data statement.json -in anchor.tsr \\
        -CAfile tsa-ca.pem -untrusted tsa.crt
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import secrets
from urllib.request import Request, urlopen

SHA256_OID = bytes.fromhex("0609608648016503040201")  # 2.16.840.1.101.3.4.2.1


class TimestampError(RuntimeError):
    pass


# ------------------------------------------------------------------ DER out


def _len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def _tlv(tag: int, content: bytes) -> bytes:
    return bytes([tag]) + _len(len(content)) + content


def _int(value: int) -> bytes:
    body = value.to_bytes(max(1, (value.bit_length() + 8) // 8), "big", signed=True)
    return _tlv(0x02, body)


def build_request(digest: bytes, nonce: int) -> bytes:
    if len(digest) != 32:
        raise ValueError("RFC 3161 request expects a SHA-256 digest")
    algorithm = _tlv(0x30, SHA256_OID + b"\x05\x00")
    imprint = _tlv(0x30, algorithm + _tlv(0x04, digest))
    cert_req = b"\x01\x01\xff"
    return _tlv(0x30, _int(1) + imprint + _int(nonce) + cert_req)


# ------------------------------------------------------------------- DER in


def _read(data: bytes, offset: int) -> tuple[int, bytes, int]:
    """Return (tag, content, next_offset) for the TLV at ``offset``."""
    tag = data[offset]
    length = data[offset + 1]
    offset += 2
    if length & 0x80:
        count = length & 0x7F
        length = int.from_bytes(data[offset : offset + count], "big")
        offset += count
    end = offset + length
    if end > len(data):
        raise TimestampError("truncated DER")
    return tag, data[offset:end], end


def _children(content: bytes) -> list[tuple[int, bytes]]:
    items, offset = [], 0
    while offset < len(content):
        tag, value, offset = _read(content, offset)
        items.append((tag, value))
    return items


@dataclass
class TokenInfo:
    status: int
    hashed_message: bytes
    nonce: int | None
    gen_time: str
    token_der: bytes


def parse_response(response: bytes) -> TokenInfo:
    tag, body, _ = _read(response, 0)
    if tag != 0x30:
        raise TimestampError("not a TimeStampResp")
    parts = _children(body)
    status = int.from_bytes(_children(parts[0][1])[0][1], "big", signed=True)
    if status not in (0, 1) or len(parts) < 2:
        raise TimestampError(f"TSA refused the request (PKIStatus {status})")
    token_der = _tlv(parts[1][0], parts[1][1])
    # ContentInfo -> [0] SignedData -> encapContentInfo -> [0] OCTET STRING TSTInfo
    content_info = _children(parts[1][1])
    signed_data = _children(_children(content_info[1][1])[0][1])
    encap = next(v for t, v in signed_data if t == 0x30 and _children(v)[0][0] == 0x06)
    econtent = _children(encap)[1][1]
    tst_der = _children(econtent)[0][1]  # OCTET STRING content = TSTInfo DER
    tst = _children(_children(tst_der)[0][1])
    imprint = _children(tst[2][1])
    hashed = imprint[1][1]
    gen_time = next(v for t, v in tst if t == 0x18).decode("ascii")
    ints_after_serial = [v for t, v in tst[4:] if t == 0x02]
    nonce = int.from_bytes(ints_after_serial[0], "big", signed=True) if ints_after_serial else None
    return TokenInfo(status, hashed, nonce, gen_time, token_der)


def request_timestamp(url: str, data: bytes, timeout: int = 20) -> TokenInfo:
    digest = hashlib.sha256(data).digest()
    nonce = secrets.randbits(63)
    request = Request(
        url,
        data=build_request(digest, nonce),
        headers={"Content-Type": "application/timestamp-query"},
    )
    with urlopen(request, timeout=timeout) as response:
        info = parse_response(response.read())
    if info.hashed_message != digest:
        raise TimestampError("TSA token does not cover the anchored statement")
    if info.nonce is not None and info.nonce != nonce:
        raise TimestampError("TSA token nonce mismatch")
    return info
