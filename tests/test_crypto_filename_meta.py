"""Tests for the filename-encryption + bucket-key + metadata cipher methods.

These cover the AES-256-GCM metadata cipher (encrypt_meta/decrypt_meta)
and the deterministic bucket key derivation used throughout the protocol.
"""
import os


from services.crypto import crypto_service


VALID_MNEMONIC = ("abandon abandon abandon abandon abandon abandon "
                  "abandon abandon abandon abandon abandon about")
BUCKET_ID = "00" * 12  # 24 hex chars


# ---------- generate_bucket_key ----------

def test_generate_bucket_key_is_deterministic():
    a = crypto_service.generate_bucket_key(VALID_MNEMONIC, BUCKET_ID)
    b = crypto_service.generate_bucket_key(VALID_MNEMONIC, BUCKET_ID)
    assert a == b


def test_generate_bucket_key_returns_64_hex_chars():
    """Truncated to 64 hex chars (32 bytes) for AES key compatibility."""
    out = crypto_service.generate_bucket_key(VALID_MNEMONIC, BUCKET_ID)
    assert len(out) == 64
    int(out, 16)  # parses as hex


def test_generate_bucket_key_differs_per_bucket_id():
    a = crypto_service.generate_bucket_key(VALID_MNEMONIC, BUCKET_ID)
    b = crypto_service.generate_bucket_key(VALID_MNEMONIC, "ff" * 12)
    assert a != b


def test_generate_bucket_key_differs_per_mnemonic():
    other_mnemonic = ("abandon abandon abandon abandon abandon abandon "
                      "abandon abandon abandon abandon abandon ability")  # last word changed
    a = crypto_service.generate_bucket_key(VALID_MNEMONIC, BUCKET_ID)
    b = crypto_service.generate_bucket_key(other_mnemonic, BUCKET_ID)
    assert a != b


# ---------- generate_filename_encryption_key ----------

def test_filename_encryption_key_is_32_bytes():
    bucket_key = "ab" * 32  # 64 hex chars
    out = crypto_service.generate_filename_encryption_key(bucket_key)
    assert isinstance(out, bytes)
    assert len(out) == 32  # AES-256 key size


def test_filename_encryption_key_deterministic_per_bucket():
    bucket_key = "cd" * 32
    a = crypto_service.generate_filename_encryption_key(bucket_key)
    b = crypto_service.generate_filename_encryption_key(bucket_key)
    assert a == b


def test_filename_encryption_key_differs_per_bucket():
    a = crypto_service.generate_filename_encryption_key("ab" * 32)
    b = crypto_service.generate_filename_encryption_key("cd" * 32)
    assert a != b


# ---------- encrypt_meta / decrypt_meta round-trip ----------

def test_encrypt_meta_decrypt_meta_roundtrip():
    """Round-trip an arbitrary string through AES-256-GCM."""
    key = os.urandom(32)
    iv = os.urandom(32)
    plaintext = "hello world"
    encrypted = crypto_service.encrypt_meta(plaintext, key, iv)

    # decrypt_meta expects key as hex
    decrypted = crypto_service.decrypt_meta(encrypted, key.hex())
    assert decrypted == plaintext


def test_encrypt_meta_returns_base64_string():
    key = os.urandom(32)
    iv = os.urandom(32)
    out = crypto_service.encrypt_meta("test", key, iv)
    import base64
    # Must be valid base64
    decoded = base64.b64decode(out)
    # Format: 16-byte auth tag + 32-byte iv + ciphertext
    assert len(decoded) >= 16 + 32 + 4  # tag + iv + at least "test"


def test_encrypt_meta_unicode_roundtrip():
    """Non-ASCII text must round-trip cleanly through UTF-8."""
    key = os.urandom(32)
    iv = os.urandom(32)
    for plaintext in ("résumé", "中文文档", "emoji 🚀 test", ""):
        enc = crypto_service.encrypt_meta(plaintext, key, iv)
        dec = crypto_service.decrypt_meta(enc, key.hex())
        assert dec == plaintext


def test_decrypt_meta_with_wrong_key_returns_none():
    """GCM auth tag mismatch must yield None, not raise."""
    key1 = os.urandom(32)
    key2 = os.urandom(32)
    iv = os.urandom(32)
    enc = crypto_service.encrypt_meta("secret", key1, iv)
    out = crypto_service.decrypt_meta(enc, key2.hex())
    assert out is None


def test_decrypt_meta_truncated_buffer_returns_none():
    """Buffer too short to contain auth tag + iv → None."""
    import base64
    short = base64.b64encode(b"too short").decode('ascii')
    out = crypto_service.decrypt_meta(short, "00" * 32)
    assert out is None


# ---------- encrypt_filename / decrypt_filename ----------

def test_encrypt_decrypt_filename_roundtrip():
    """Full filename encryption protocol round-trip."""
    encrypted = crypto_service.encrypt_filename(VALID_MNEMONIC, BUCKET_ID, "myfile.txt")
    # Encrypted is a base64 string
    assert encrypted
    assert encrypted != "myfile.txt"

    decrypted = crypto_service.decrypt_filename(VALID_MNEMONIC, BUCKET_ID, encrypted)
    assert decrypted == "myfile.txt"


def test_encrypt_filename_deterministic():
    """Filename encryption uses a deterministic IV (so the same filename
    in the same bucket always produces the same ciphertext) — important
    for server-side dedup/lookup."""
    a = crypto_service.encrypt_filename(VALID_MNEMONIC, BUCKET_ID, "doc.pdf")
    b = crypto_service.encrypt_filename(VALID_MNEMONIC, BUCKET_ID, "doc.pdf")
    assert a == b


def test_encrypt_filename_differs_per_filename():
    a = crypto_service.encrypt_filename(VALID_MNEMONIC, BUCKET_ID, "a.txt")
    b = crypto_service.encrypt_filename(VALID_MNEMONIC, BUCKET_ID, "b.txt")
    assert a != b


def test_encrypt_filename_differs_per_bucket():
    a = crypto_service.encrypt_filename(VALID_MNEMONIC, BUCKET_ID, "x.txt")
    b = crypto_service.encrypt_filename(VALID_MNEMONIC, "ff" * 12, "x.txt")
    assert a != b


def test_decrypt_filename_with_wrong_bucket_returns_none():
    """If the bucket id changed (e.g., file moved), decryption fails cleanly."""
    enc = crypto_service.encrypt_filename(VALID_MNEMONIC, BUCKET_ID, "x.txt")
    out = crypto_service.decrypt_filename(VALID_MNEMONIC, "ff" * 12, enc)
    assert out is None


def test_decrypt_filename_handles_unicode():
    for filename in ("résumé.pdf", "文档.txt", "🚀.bin"):
        enc = crypto_service.encrypt_filename(VALID_MNEMONIC, BUCKET_ID, filename)
        dec = crypto_service.decrypt_filename(VALID_MNEMONIC, BUCKET_ID, enc)
        assert dec == filename
