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


# --- Login key generation (real OpenPGP keys, validated server-side) ---

def test_generate_keys_shape():
    """Login payload must carry ecc + kyber maps (server requires both)."""
    keys = crypto_service.generate_keys("any-password")
    assert isinstance(keys["publicKey"], str)
    assert isinstance(keys["privateKeyEncrypted"], str)
    assert isinstance(keys["ecc"], dict)
    assert isinstance(keys["ecc"]["publicKey"], str)
    assert isinstance(keys["ecc"]["privateKeyEncrypted"], str)
    assert isinstance(keys["kyber"], dict)


def test_generate_keys_public_key_is_base64_armored_openpgp():
    """ecc.publicKey is base64 of an armored OpenPGP public key block."""
    import base64
    keys = crypto_service.generate_keys("pw")
    armored = base64.b64decode(keys["ecc"]["publicKey"]).decode("utf-8")
    assert armored.startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----")


def test_generate_keys_private_key_uses_internxt_gcm_envelope():
    """privateKeyEncrypted is AES-256-GCM: base64(salt[64]+iv[16]+tag[16]+ct)."""
    import base64
    from config.config import config_service
    keys = crypto_service.generate_keys("pw")
    raw = base64.b64decode(keys["ecc"]["privateKeyEncrypted"])
    assert len(raw) > 96
    assert raw[:64].hex() == config_service.get("APP_MAGIC_SALT")
    assert raw[64:80].hex() == config_service.get("APP_MAGIC_IV")


def test_generate_keys_private_key_roundtrips():
    """The encrypted private key decrypts back to an armored PGP private key."""
    import base64
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    password = "test-pass"
    keys = crypto_service.generate_keys(password)
    raw = base64.b64decode(keys["ecc"]["privateKeyEncrypted"])
    salt, iv, tag, ct = raw[:64], raw[64:80], raw[80:96], raw[96:]
    key = PBKDF2HMAC(algorithm=hashes.SHA512(), length=32, salt=salt,
                     iterations=2145).derive(password.encode())
    decrypted = AESGCM(key).decrypt(iv, ct + tag, None).decode("utf-8")
    assert decrypted.startswith("-----BEGIN PGP PRIVATE KEY BLOCK-----")
