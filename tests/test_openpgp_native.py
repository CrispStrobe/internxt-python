"""Tests for services/openpgp_native.py and the crypto backend dispatch.

The native serializer is also validated end-to-end against openpgp.js (the
server's library) during development; these tests give in-process regression
protection with no external dependency, by parsing the emitted packets and
independently verifying the EdDSA self-certification signature.
"""
import base64
import struct

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.exceptions import InvalidSignature

from services import openpgp_native as N
from services.crypto import crypto_service


# --- minimal, independent OpenPGP helpers (not imported from the module) ----

def _dearmor(text: str) -> bytes:
    lines = text.strip().splitlines()
    body = [ln for ln in lines[1:-1] if ln and not ln.startswith("=")]
    return base64.b64decode("".join(body))


def _parse_packets(data: bytes):
    """Parse a stream of new-format packets into (tag, body) pairs."""
    out, i = [], 0
    while i < len(data):
        assert data[i] & 0xC0 == 0xC0, "expected new-format packet"
        tag = data[i] & 0x3F
        i += 1
        first = data[i]
        if first < 192:
            length, i = first, i + 1
        elif first < 224:
            length, i = ((first - 192) << 8) + data[i + 1] + 192, i + 2
        elif first == 255:
            length, i = struct.unpack(">I", data[i + 1:i + 5])[0], i + 5
        else:
            raise AssertionError("partial body lengths not expected")
        out.append((tag, data[i:i + length]))
        i += length
    return out


def _read_mpi(body: bytes, off: int):
    bits = struct.unpack(">H", body[off:off + 2])[0]
    n = (bits + 7) // 8
    return body[off + 2:off + 2 + n], off + 2 + n


def _ed25519_pub_from_body(pub_body: bytes) -> bytes:
    # version(1) ctime(4) algo(1) oidlen(1) oid(9) then MPI(0x40 || 32-byte key)
    oidlen = pub_body[6]
    mpi, _ = _read_mpi(pub_body, 7 + oidlen)
    assert mpi[0] == 0x40
    return mpi[1:]


# ---------------------------------------------------------------------------

def test_armor_markers_and_crc():
    pub, priv = N.generate_ed25519legacy_keypair()
    assert pub.startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----")
    assert pub.rstrip().endswith("-----END PGP PUBLIC KEY BLOCK-----")
    assert priv.startswith("-----BEGIN PGP PRIVATE KEY BLOCK-----")
    # CRC-24 line must match the recomputed checksum of the packet bytes.
    for text in (pub, priv):
        raw = _dearmor(text)
        crc_line = [ln for ln in text.strip().splitlines() if ln.startswith("=")][0]
        expected = base64.b64encode(N._crc24(raw).to_bytes(3, "big")).decode()
        assert crc_line == "=" + expected


def test_public_key_packet_structure():
    pub, _ = N.generate_ed25519legacy_keypair()
    tags = [t for t, _ in _parse_packets(_dearmor(pub))]
    # transferable public key: pubkey, uid, cert-sig, subkey, binding-sig
    assert tags == [6, 13, 2, 14, 2]


def test_private_key_packet_structure():
    _, priv = N.generate_ed25519legacy_keypair()
    tags = [t for t, _ in _parse_packets(_dearmor(priv))]
    assert tags == [5, 13, 2, 7, 2]


def test_self_certification_signature_verifies():
    """The primary key's self-cert signature must verify under its own Ed25519
    public key — the property the server checks."""
    pub, _ = N.generate_ed25519legacy_keypair(uid="inxt@inxt.com", creation_time=1700000000)
    packets = _parse_packets(_dearmor(pub))
    pub_body = packets[0][1]
    uid = packets[1][1]
    sig = packets[2][1]

    ed_pub = _ed25519_pub_from_body(pub_body)

    # Signature body: version, type, pkalgo, hashalgo, hashed_len(2), hashed...
    assert sig[0] == 0x04 and sig[1] == 0x13 and sig[2] == 0x16 and sig[3] == 0x08
    hashed_len = struct.unpack(">H", sig[4:6])[0]
    sig_hashed = sig[:6 + hashed_len]
    off = 6 + hashed_len
    unhashed_len = struct.unpack(">H", sig[off:off + 2])[0]
    off += 2 + unhashed_len
    off += 2  # skip the 2-byte left-hash
    r, off = _read_mpi(sig, off)
    s, off = _read_mpi(sig, off)
    signature = r.rjust(32, b"\x00") + s.rjust(32, b"\x00")

    import hashlib
    hashdata = (
        b"\x99" + struct.pack(">H", len(pub_body)) + pub_body
        + b"\xB4" + struct.pack(">I", len(uid)) + uid
        + sig_hashed
        + b"\x04\xFF" + struct.pack(">I", len(sig_hashed))
    )
    digest = hashlib.sha256(hashdata).digest()
    # Raises InvalidSignature if the self-cert is malformed.
    Ed25519PublicKey.from_public_bytes(ed_pub).verify(signature, digest)


def test_tampered_signature_fails():
    pub, _ = N.generate_ed25519legacy_keypair()
    packets = _parse_packets(_dearmor(pub))
    ed_pub = _ed25519_pub_from_body(packets[0][1])
    with pytest.raises(InvalidSignature):
        Ed25519PublicKey.from_public_bytes(ed_pub).verify(b"\x00" * 64, b"\x00" * 32)


def test_keys_are_unique_per_call():
    a, _ = N.generate_ed25519legacy_keypair()
    b, _ = N.generate_ed25519legacy_keypair()
    assert a != b


# --- backend dispatch -------------------------------------------------------

def test_dispatch_prefers_pgpy_then_falls_back(monkeypatch):
    """Order is pgpy -> native -> gnupg; a failing higher-priority backend must
    not abort the whole operation."""
    calls = []

    def boom(name):
        def _fn():
            calls.append(name)
            raise RuntimeError(f"{name} unavailable")
        return _fn

    def ok_native():
        calls.append("native")
        return "PUB", "PRIV"

    monkeypatch.setattr(crypto_service, "_openpgp_keypair_pgpy", boom("pgpy"))
    monkeypatch.setattr(crypto_service, "_openpgp_keypair_native", ok_native)
    pub, priv = crypto_service._generate_openpgp_armored_keypair()
    assert (pub, priv) == ("PUB", "PRIV")
    assert calls == ["pgpy", "native"]  # native reached only after pgpy failed


def test_dispatch_raises_when_all_backends_fail(monkeypatch):
    for name in ("_openpgp_keypair_pgpy", "_openpgp_keypair_native", "_openpgp_keypair_gnupg"):
        def _boom():
            raise RuntimeError("nope")
        monkeypatch.setattr(crypto_service, name, _boom)
    with pytest.raises(RuntimeError, match="any backend"):
        crypto_service._generate_openpgp_armored_keypair()


def test_generate_keys_uses_dispatch(monkeypatch):
    monkeypatch.setattr(
        crypto_service, "_generate_openpgp_armored_keypair",
        lambda: N.generate_ed25519legacy_keypair(),
    )
    payload = crypto_service.generate_keys("pw-123")
    assert set(payload) >= {"privateKeyEncrypted", "publicKey", "ecc"}
    decoded = base64.b64decode(payload["ecc"]["publicKey"]).decode()
    assert decoded.startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----")
