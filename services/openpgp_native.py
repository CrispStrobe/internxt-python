#!/usr/bin/env python3
"""
internxt_cli/services/openpgp_native.py

Native OpenPGP "ed25519Legacy" keypair generator, built on `cryptography` alone.

Internxt's server validates the OpenPGP keys in the login payload with openpgp.js
("keys.ecc.publicKey is not a valid OpenPGP public key"). The canonical way to
produce those keys is PGPy, but PGPy (unmaintained since 2022) imports the
`imghdr` stdlib module, which was removed in Python 3.13 — so it fails to import
at all on modern interpreters (see internxt-python issue #10).

Because the generated private key is only ever AES-GCM-encrypted and stored (it
is never used to decrypt anything in this CLI), we have full control over the
format: we only need to emit a transferable public key that openpgp.js accepts,
plus a parseable private key. This module serialises the OpenPGP v4 packets by
hand — an EdDSA/Ed25519 primary key + ECDH/Curve25519 encryption subkey, matching
openpgp.js `ed25519Legacy` — using only `cryptography` primitives. It therefore
works on every Python version the CLI supports, with no third-party OpenPGP
dependency.

Validated against openpgp.js (the server's library): the output parses, the
self-signature and subkey binding verify, and a full encrypt→decrypt round-trip
succeeds. Also imports cleanly into GnuPG 2.x.
"""
import struct
import base64
import hashlib
import time
from typing import Optional, Tuple

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives import serialization

_ENC = serialization.Encoding.Raw
_PRIV = serialization.PrivateFormat.Raw
_PUB = serialization.PublicFormat.Raw
_NOENC = serialization.NoEncryption()

# Curve OIDs, length-prefixed by the caller.
_OID_ED25519 = bytes.fromhex("2b06010401da470f01")     # 1.3.6.1.4.1.11591.15.1
_OID_CV25519 = bytes.fromhex("2b060104019755010501")   # 1.3.6.1.4.1.3029.1.5.1

# Packet tags (RFC 4880 §4.3)
_TAG_SIG = 2
_TAG_SECKEY = 5
_TAG_PUBKEY = 6
_TAG_SECSUBKEY = 7
_TAG_UID = 13
_TAG_PUBSUBKEY = 14


def _mpi(value: bytes) -> bytes:
    """Encode raw big-endian bytes as an OpenPGP MPI (2-byte bit length + minimal bytes)."""
    i = int.from_bytes(value, "big")
    if i == 0:
        return b"\x00\x00"
    bits = i.bit_length()
    return struct.pack(">H", bits) + i.to_bytes((bits + 7) // 8, "big")


def _packet(tag: int, body: bytes) -> bytes:
    """Frame a packet body with a new-format header (RFC 4880 §4.2.2)."""
    n = len(body)
    if n < 192:
        length = bytes([n])
    elif n < 8384:
        n -= 192
        length = bytes([(n >> 8) + 192, n & 0xFF])
    else:
        length = b"\xFF" + struct.pack(">I", len(body))
    return bytes([0xC0 | tag]) + length + body


def _subpkt(sp_type: int, data: bytes) -> bytes:
    """A signature subpacket (short-form length; all subpackets here are < 192 bytes)."""
    body = bytes([sp_type]) + data
    return bytes([len(body)]) + body


def _pub_body_ed25519(creation: int, ed_pub: bytes) -> bytes:
    """Public-Key packet body: v4 EdDSA over Ed25519."""
    return (
        b"\x04" + struct.pack(">I", creation) + b"\x16"        # version, ctime, algo 22 (EdDSA)
        + bytes([len(_OID_ED25519)]) + _OID_ED25519
        + _mpi(b"\x40" + ed_pub)                               # 0x40 prefix = native point form
    )


def _pub_body_ecdh(creation: int, x_pub: bytes) -> bytes:
    """Public-Subkey packet body: v4 ECDH over Curve25519 (KDF: SHA256 + AES-128 wrap)."""
    kdf = bytes([0x03, 0x01, 0x08, 0x07])                      # len, reserved, SHA256, AES128
    return (
        b"\x04" + struct.pack(">I", creation) + b"\x12"        # version, ctime, algo 18 (ECDH)
        + bytes([len(_OID_CV25519)]) + _OID_CV25519
        + _mpi(b"\x40" + x_pub)
        + kdf
    )


def _hashdata_key(pub_body: bytes) -> bytes:
    """Key material as fed into signature/fingerprint hashes (0x99 || len || body)."""
    return b"\x99" + struct.pack(">H", len(pub_body)) + pub_body


def _hashdata_uid(uid: bytes) -> bytes:
    return b"\xB4" + struct.pack(">I", len(uid)) + uid


def _fingerprint(pub_body: bytes) -> bytes:
    """v4 fingerprint = SHA-1 of the hashed key material."""
    return hashlib.sha1(_hashdata_key(pub_body)).digest()  # nosec B324 - OpenPGP v4 spec


def _signature(sig_type: int, hashed_extra: bytes, ed_key: Ed25519PrivateKey,
               prefix_hashdata: bytes, fingerprint: bytes) -> bytes:
    """Build a v4 EdDSA signature packet body (RFC 4880 §5.2)."""
    # Issuer Fingerprint (type 33) goes in the hashed area so verifiers can bind
    # the signature to the primary key; Issuer KeyID (type 16) in the unhashed area.
    hashed = hashed_extra + _subpkt(33, b"\x04" + fingerprint)
    sig_hashed = b"\x04" + bytes([sig_type]) + b"\x16\x08" + struct.pack(">H", len(hashed)) + hashed
    trailer = b"\x04\xFF" + struct.pack(">I", len(sig_hashed))
    digest = hashlib.sha256(prefix_hashdata + sig_hashed + trailer).digest()
    sig = ed_key.sign(digest)                                 # EdDSA signs the digest as its message
    unhashed = _subpkt(16, fingerprint[-8:])
    return (
        sig_hashed
        + struct.pack(">H", len(unhashed)) + unhashed
        + digest[:2]                                          # left 16 bits of the hash
        + _mpi(sig[:32]) + _mpi(sig[32:])                     # R, S
    )


def _secret_tail(secret_bytes: bytes) -> bytes:
    """S2K-usage-0 (unencrypted) secret material + 2-octet checksum."""
    mpi = _mpi(secret_bytes)
    return b"\x00" + mpi + struct.pack(">H", sum(mpi) % 65536)


_CRC24_INIT = 0xB704CE
_CRC24_POLY = 0x1864CFB


def _crc24(data: bytes) -> int:
    crc = _CRC24_INIT
    for byte in data:
        crc ^= byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= _CRC24_POLY
    return crc & 0xFFFFFF


def _armor(data: bytes, label: str) -> str:
    b64 = base64.b64encode(data).decode("ascii")
    lines = "\n".join(b64[i:i + 64] for i in range(0, len(b64), 64))
    crc = base64.b64encode(_crc24(data).to_bytes(3, "big")).decode("ascii")
    return (
        f"-----BEGIN PGP {label}-----\n\n"
        f"{lines}\n=" + crc + "\n"
        f"-----END PGP {label}-----\n"
    )


def generate_ed25519legacy_keypair(uid: str = "inxt@inxt.com",
                                   creation_time: Optional[int] = None) -> Tuple[str, str]:
    """
    Generate an OpenPGP ed25519Legacy keypair.

    Returns (public_key_armored, private_key_armored). The private key is emitted
    unencrypted (S2K usage 0); callers are expected to wrap it themselves (this
    CLI AES-256-GCM-encrypts the armored blob before sending it).
    """
    if creation_time is None:
        creation_time = int(time.time())

    ed = Ed25519PrivateKey.generate()
    ed_seed = ed.private_bytes(_ENC, _PRIV, _NOENC)
    ed_pub = ed.public_key().public_bytes(_ENC, _PUB)

    x = X25519PrivateKey.generate()
    x_seed = x.private_bytes(_ENC, _PRIV, _NOENC)
    x_pub = x.public_key().public_bytes(_ENC, _PUB)

    prim_body = _pub_body_ed25519(creation_time, ed_pub)
    sub_body = _pub_body_ecdh(creation_time, x_pub)
    uid_bytes = uid.encode("utf-8")
    fp = _fingerprint(prim_body)

    ctime = _subpkt(2, struct.pack(">I", creation_time))      # signature creation time
    cert_sig = _signature(                                     # positive certification of the UID
        0x13, ctime + _subpkt(27, bytes([0x03])), ed,         # key flags: sign | certify
        _hashdata_key(prim_body) + _hashdata_uid(uid_bytes), fp,
    )
    bind_sig = _signature(                                     # subkey binding
        0x18, ctime + _subpkt(27, bytes([0x0C])), ed,         # key flags: encrypt comms | storage
        _hashdata_key(prim_body) + _hashdata_key(sub_body), fp,
    )

    public = (
        _packet(_TAG_PUBKEY, prim_body)
        + _packet(_TAG_UID, uid_bytes)
        + _packet(_TAG_SIG, cert_sig)
        + _packet(_TAG_PUBSUBKEY, sub_body)
        + _packet(_TAG_SIG, bind_sig)
    )
    # OpenPGP stores the Curve25519 secret scalar big-endian (reverse of the raw
    # little-endian X25519 scalar) — the well-known "curve25519 legacy" quirk.
    private = (
        _packet(_TAG_SECKEY, prim_body + _secret_tail(ed_seed))
        + _packet(_TAG_UID, uid_bytes)
        + _packet(_TAG_SIG, cert_sig)
        + _packet(_TAG_SECSUBKEY, sub_body + _secret_tail(x_seed[::-1]))
        + _packet(_TAG_SIG, bind_sig)
    )
    return _armor(public, "PUBLIC KEY BLOCK"), _armor(private, "PRIVATE KEY BLOCK")
