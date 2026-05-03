"""Tests for services/crypto.py."""
import os


from services.crypto import crypto_service


def test_encrypt_text_roundtrip():
    """encrypt_text -> decrypt_text must round-trip arbitrary text."""
    for text in ("hello", "", "unicode ✓ 中文", "x" * 256, "a\nb\rc\td"):
        encrypted = crypto_service.encrypt_text(text)
        assert crypto_service.decrypt_text(encrypted) == text


def test_encrypt_text_uses_random_salt():
    """Two encryptions of the same text must differ (random salt)."""
    a = crypto_service.encrypt_text("same plaintext")
    b = crypto_service.encrypt_text("same plaintext")
    assert a != b


def test_pass_to_hash_deterministic_with_salt():
    """Same password+salt must yield same hash."""
    salt = os.urandom(16).hex()
    h1 = crypto_service.pass_to_hash("password123", salt)
    h2 = crypto_service.pass_to_hash("password123", salt)
    assert h1 == h2
    assert h1['salt'] == salt
    # PBKDF2-HMAC-SHA1, 32 bytes -> 64 hex chars
    assert len(h1['hash']) == 64


def test_pass_to_hash_different_passwords_differ():
    salt = "00" * 16
    h1 = crypto_service.pass_to_hash("password1", salt)
    h2 = crypto_service.pass_to_hash("password2", salt)
    assert h1['hash'] != h2['hash']


def test_pass_to_hash_no_salt_generates_one():
    h = crypto_service.pass_to_hash("pw", None)
    assert h['salt'] and len(h['salt']) == 32  # 16 bytes hex


def test_aes_ctr_internxt_protocol_roundtrip():
    """File encryption/decryption round-trip for various sizes."""
    mnemonic = ("abandon abandon abandon abandon abandon abandon abandon "
                "abandon abandon abandon abandon about")
    bucket_id = "00" * 12  # 24 hex chars (standard bucket id length)
    for size in (0, 1, 16, 1024, 64 * 1024, 1024 * 1024):
        plaintext = os.urandom(size)
        encrypted, file_index_hex = crypto_service.encrypt_stream_internxt_protocol(
            plaintext, mnemonic, bucket_id
        )
        # Same length (CTR mode is a stream cipher)
        assert len(encrypted) == len(plaintext)
        decrypted = crypto_service.decrypt_stream_internxt_protocol(
            encrypted, mnemonic, bucket_id, file_index_hex
        )
        assert decrypted == plaintext


def test_decrypt_meta_returns_none_on_garbage():
    """Bad input must return None, not raise."""
    assert crypto_service.decrypt_meta("not-valid-base64!!", "00" * 32) is None
    assert crypto_service.decrypt_meta("dGVzdA==", "00" * 32) is None  # too short


def test_validate_mnemonic_known_good():
    valid = ("abandon abandon abandon abandon abandon abandon abandon "
             "abandon abandon abandon abandon about")
    assert crypto_service.validate_mnemonic(valid) is True


def test_validate_mnemonic_known_bad():
    assert crypto_service.validate_mnemonic("not a real mnemonic") is False


def test_generate_filename_encryption_iv_does_not_crash():
    """Regression: hmac.new(key, hashlib.sha512) was passing sha512 as msg
    instead of digestmod, which raised TypeError at runtime."""
    bucket_key = "ab" * 32  # 64 hex chars
    iv = crypto_service.generate_filename_encryption_iv(bucket_key, "bid", "filename.txt")
    assert isinstance(iv, bytes)
    assert len(iv) == 32


def test_generate_filename_encryption_iv_deterministic():
    bucket_key = "ab" * 32
    iv1 = crypto_service.generate_filename_encryption_iv(bucket_key, "bid", "name.txt")
    iv2 = crypto_service.generate_filename_encryption_iv(bucket_key, "bid", "name.txt")
    assert iv1 == iv2
    iv3 = crypto_service.generate_filename_encryption_iv(bucket_key, "bid", "other.txt")
    assert iv3 != iv1
