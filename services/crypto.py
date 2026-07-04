#!/usr/bin/env python3
"""
internxt_cli/services/crypto.py
Cryptographic operations for Internxt CLI
"""
import os
import sys
import hashlib
import base64
import hmac
from typing import Tuple, Dict, Any, Optional
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.backends import default_backend
from mnemonic import Mnemonic

from config.config import config_service

# Diagnostics are off by default and go to stderr when enabled (mirrors auth.py),
# so they never pollute stdout. Enable with INTERNXT_DEBUG=1 (or true/yes/on).
_DEBUG = os.environ.get('INTERNXT_DEBUG', '').strip().lower() in (
    '1', 'true', 'yes', 'on')


def _dbg(message: str) -> None:
    if _DEBUG:
        print(message, file=sys.stderr)


# constants from inxt-js crypto.ts
BUCKET_META_MAGIC = bytes([
    66, 150, 71, 16, 50, 114, 88, 160, 163, 35, 154, 65, 162, 213, 226, 215,
    70, 138, 57, 61, 52, 19, 210, 170, 38, 164, 162, 200, 86, 201, 2, 81
])


# --- RIPEMD-160 ------------------------------------------------------------
# Internxt's network protocol uses ripemd160(sha256(ciphertext)) as the shard
# content hash (see inxt-js src/lib/utils/streams/Hasher.ts). hashlib usually
# provides ripemd160, but on systems where OpenSSL 3.0 disables the legacy
# provider (common in slim Docker images) hashlib.new('ripemd160') raises. We
# fall back to this self-contained pure-Python implementation in that case.

_RMD_R = [
    0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15,
    7, 4, 13, 1, 10, 6, 15, 3, 12, 0, 9, 5, 2, 14, 11, 8,
    3, 10, 14, 4, 9, 15, 8, 1, 2, 7, 0, 6, 13, 11, 5, 12,
    1, 9, 11, 10, 0, 8, 12, 4, 13, 3, 7, 15, 14, 5, 6, 2,
    4, 0, 5, 9, 7, 12, 2, 10, 14, 1, 3, 8, 11, 6, 15, 13,
]
_RMD_RP = [
    5, 14, 7, 0, 9, 2, 11, 4, 13, 6, 15, 8, 1, 10, 3, 12,
    6, 11, 3, 7, 0, 13, 5, 10, 14, 15, 8, 12, 4, 9, 1, 2,
    15, 5, 1, 3, 7, 14, 6, 9, 11, 8, 12, 2, 10, 0, 4, 13,
    8, 6, 4, 1, 3, 11, 15, 0, 5, 12, 2, 13, 9, 7, 10, 14,
    12, 15, 10, 4, 1, 5, 8, 7, 6, 2, 13, 14, 0, 3, 9, 11,
]
_RMD_S = [
    11, 14, 15, 12, 5, 8, 7, 9, 11, 13, 14, 15, 6, 7, 9, 8,
    7, 6, 8, 13, 11, 9, 7, 15, 7, 12, 15, 9, 11, 7, 13, 12,
    11, 13, 6, 7, 14, 9, 13, 15, 14, 8, 13, 6, 5, 12, 7, 5,
    11, 12, 14, 15, 14, 15, 9, 8, 9, 14, 5, 6, 8, 6, 5, 12,
    9, 15, 5, 11, 6, 8, 13, 12, 5, 12, 13, 14, 11, 8, 5, 6,
]
_RMD_SP = [
    8, 9, 9, 11, 13, 15, 15, 5, 7, 7, 8, 11, 14, 14, 12, 6,
    9, 13, 15, 7, 12, 8, 9, 11, 7, 7, 12, 7, 6, 15, 13, 11,
    9, 7, 15, 11, 8, 6, 6, 14, 12, 13, 5, 14, 13, 13, 7, 5,
    15, 5, 8, 11, 14, 14, 6, 14, 6, 9, 12, 9, 12, 5, 15, 8,
    8, 5, 12, 9, 12, 5, 14, 6, 8, 13, 6, 5, 15, 13, 11, 11,
]
_RMD_K = [0x00000000, 0x5A827999, 0x6ED9EBA1, 0x8F1BBCDC, 0xA953FD4E]
_RMD_KP = [0x50A28BE6, 0x5C4DD124, 0x6D703EF3, 0x7A6D76E9, 0x00000000]


def _rmd_rol(x: int, n: int) -> int:
    return ((x << n) | (x >> (32 - n))) & 0xFFFFFFFF


def _rmd_f(j: int, x: int, y: int, z: int) -> int:
    if j < 16:
        return x ^ y ^ z
    if j < 32:
        return (x & y) | (~x & z)
    if j < 48:
        return (x | ~y) ^ z
    if j < 64:
        return (x & z) | (y & ~z)
    return x ^ (y | ~z)


def ripemd160_pure(data: bytes) -> bytes:
    """Self-contained RIPEMD-160. Used only as a fallback when hashlib lacks it."""
    import struct

    h0, h1, h2, h3, h4 = 0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0

    msg = bytearray(data)
    bit_len = (8 * len(data)) & 0xFFFFFFFFFFFFFFFF
    msg.append(0x80)
    while len(msg) % 64 != 56:
        msg.append(0x00)
    msg += struct.pack('<Q', bit_len)

    for off in range(0, len(msg), 64):
        x = struct.unpack('<16I', bytes(msg[off:off + 64]))
        al, bl, cl, dl, el = h0, h1, h2, h3, h4
        ar, br, cr, dr, er = h0, h1, h2, h3, h4
        for j in range(80):
            rnd = j // 16
            t = (_rmd_rol((al + _rmd_f(j, bl, cl, dl) + x[_RMD_R[j]] + _RMD_K[rnd]) & 0xFFFFFFFF, _RMD_S[j]) + el) & 0xFFFFFFFF
            al, el, dl, cl, bl = el, dl, _rmd_rol(cl, 10), bl, t
            t = (_rmd_rol((ar + _rmd_f(79 - j, br, cr, dr) + x[_RMD_RP[j]] + _RMD_KP[rnd]) & 0xFFFFFFFF, _RMD_SP[j]) + er) & 0xFFFFFFFF
            ar, er, dr, cr, br = er, dr, _rmd_rol(cr, 10), br, t
        t = (h1 + cl + dr) & 0xFFFFFFFF
        h1 = (h2 + dl + er) & 0xFFFFFFFF
        h2 = (h3 + el + ar) & 0xFFFFFFFF
        h3 = (h4 + al + br) & 0xFFFFFFFF
        h4 = (h0 + bl + cr) & 0xFFFFFFFF
        h0 = t

    return struct.pack('<5I', h0, h1, h2, h3, h4)


def ripemd160(data: bytes) -> bytes:
    """ripemd160 via hashlib, falling back to the pure-Python implementation."""
    try:
        h = hashlib.new('ripemd160')
        h.update(data)
        return h.digest()
    except (ValueError, TypeError):
        return ripemd160_pure(data)

class CryptoService:
    """Handles all cryptographic operations"""

    def __init__(self):
        self.backend = default_backend()
        self.mnemonic_gen = Mnemonic("english")

    def generate_file_key(self, mnemonic: str, bucket_id: str, index: bytes) -> bytes:
        """
        Generate file key using Internxt's deterministic key derivation
        This matches the TypeScript implementation exactly
        """
        # Generate bucket key first
        bucket_key = self.generate_file_bucket_key(mnemonic, bucket_id)
        
        # Generate file key from bucket key and index
        return self.get_file_deterministic_key(bucket_key[:32], index)[:32]

    def generate_file_bucket_key(self, mnemonic: str, bucket_id: str) -> bytes:
        """
        Generate bucket key from mnemonic and bucket ID
        """
        # Convert mnemonic to seed
        seed = self.mnemonic_gen.to_seed(mnemonic)
        
        # Convert bucket ID from hex string to bytes
        bucket_id_bytes = bytes.fromhex(bucket_id)
        
        # Generate deterministic key
        return self.get_file_deterministic_key(seed, bucket_id_bytes)

    def get_file_deterministic_key(self, key: bytes, data: bytes) -> bytes:
        """
        Generate deterministic key using SHA-512
        """
        hash_obj = hashlib.sha512()
        hash_obj.update(key)
        hash_obj.update(data)
        return hash_obj.digest()

    def encrypt_stream_internxt_protocol(self, data: bytes, mnemonic: str, bucket_id: str) -> Tuple[bytes, str]:
        """
        Encrypts file data matching Internxt protocol
        Returns (encrypted_data, file_index_hex)
        """
        # Generate 32-byte random index
        index = os.urandom(32)
        
        # Generate file key
        file_key = self.generate_file_key(mnemonic, bucket_id, index)
        
        # Use first 16 bytes of index as IV
        iv = index[:16]
        
        # Encrypt using AES-256-CTR
        cipher = Cipher(algorithms.AES(file_key), modes.CTR(iv), backend=self.backend)
        encryptor = cipher.encryptor()
        encrypted_data = encryptor.update(data) + encryptor.finalize()
        
        return encrypted_data, index.hex()

    def new_upload_cipher(self, mnemonic: str, bucket_id: str) -> Tuple[Any, str]:
        """
        Create a stateful AES-256-CTR encryptor for a streaming upload.

        Returns (encryptor, file_index_hex). Feed plaintext chunks via
        ``encryptor.update(chunk)`` in order; because CTR is one continuous
        keystream, the concatenated ciphertext is identical to encrypting the
        whole file at once (and the same length). This lets us slice the
        ciphertext into multipart parts without resetting the counter — exactly
        what the official clients do (inxt-js multipart.ts / drive-web).
        """
        index = os.urandom(32)
        file_key = self.generate_file_key(mnemonic, bucket_id, index)
        iv = index[:16]
        cipher = Cipher(algorithms.AES(file_key), modes.CTR(iv), backend=self.backend)
        return cipher.encryptor(), index.hex()

    def shard_hash_from_sha256(self, sha256_digest: bytes) -> str:
        """
        Compute the network shard content hash from a completed sha256 digest.

        Internxt's protocol stores ripemd160(sha256(ciphertext)) as the shard
        hash (40 hex chars), not plain sha256. Callers stream the ciphertext
        through a hashlib.sha256() object and pass its .digest() here.
        """
        return ripemd160(sha256_digest).hex()

    def new_download_decryptor(self, mnemonic: str, bucket_id: str, file_index_hex: str) -> Any:
        """
        Create a stateful AES-256-CTR decryptor for a streaming download.

        Feed ciphertext chunks via ``decryptor.update(chunk)`` in order; the
        concatenated plaintext is identical to decrypting the whole file at
        once. Mirrors new_upload_cipher and lets us decrypt-to-disk without
        holding the whole file in memory.
        """
        index = bytes.fromhex(file_index_hex)
        file_key = self.generate_file_key(mnemonic, bucket_id, index)
        iv = index[:16]
        cipher = Cipher(algorithms.AES(file_key), modes.CTR(iv), backend=self.backend)
        return cipher.decryptor()

    def new_download_decryptor_at(self, mnemonic: str, bucket_id: str,
                                  file_index_hex: str, offset: int) -> Any:
        """
        Create an AES-256-CTR decryptor whose keystream is positioned at a
        16-byte-aligned byte ``offset`` into the file.

        AES-CTR is seekable: the counter block for byte offset O is
        ``initial_counter + O // 16`` (the 16-byte IV interpreted as a 128-bit
        big-endian integer, advanced by O/16 blocks, with 128-bit wraparound).
        This lets each ranged-download worker decrypt its slice independently of
        the others. ``offset`` MUST be a multiple of 16 (the AES block size).
        """
        if offset % 16 != 0:
            raise ValueError(f"CTR seek offset must be 16-byte aligned, got {offset}")
        index = bytes.fromhex(file_index_hex)
        file_key = self.generate_file_key(mnemonic, bucket_id, index)
        iv = index[:16]
        counter = (int.from_bytes(iv, 'big') + (offset // 16)) & ((1 << 128) - 1)
        seeked_iv = counter.to_bytes(16, 'big')
        cipher = Cipher(algorithms.AES(file_key), modes.CTR(seeked_iv), backend=self.backend)
        return cipher.decryptor()

    def decrypt_stream_internxt_protocol(self, encrypted_data: bytes, mnemonic: str,
                                       bucket_id: str, file_index_hex: str) -> bytes:
        """
        Decrypts file data using Internxt protocol
        FIXED: Now properly handles the decryption
        """
        # Convert index from hex to bytes
        index = bytes.fromhex(file_index_hex)
        
        # Generate file key using the same method as encryption
        file_key = self.generate_file_key(mnemonic, bucket_id, index)
        
        # Use first 16 bytes of index as IV
        iv = index[:16]
        
        # Decrypt using AES-256-CTR
        cipher = Cipher(algorithms.AES(file_key), modes.CTR(iv), backend=self.backend)
        decryptor = cipher.decryptor()
        decrypted_data = decryptor.update(encrypted_data) + decryptor.finalize()
        
        return decrypted_data

    def encrypt_filename(self, mnemonic: str, bucket_id: str, filename: str) -> str:
        """
        Encrypt filename using Internxt protocol
        """
        bucket_key = self.generate_bucket_key(mnemonic, bucket_id)
        
        # Generate encryption key using BUCKET_META_MAGIC
        encryption_key = self.generate_filename_encryption_key(bucket_key)
        
        # Generate encryption IV
        encryption_iv = self.generate_filename_encryption_iv(bucket_key, bucket_id, filename)
        
        return self.encrypt_meta(filename, encryption_key, encryption_iv)

    def decrypt_filename(self, mnemonic: str, bucket_id: str, encrypted_name: str) -> Optional[str]:
        """
        Decrypt filename using Internxt protocol
        """
        bucket_key = self.generate_bucket_key(mnemonic, bucket_id)

        # Generate decryption key using BUCKET_META_MAGIC
        key = hmac.new(
            bytes.fromhex(bucket_key),
            BUCKET_META_MAGIC,
            hashlib.sha512
        ).hexdigest()

        return self.decrypt_meta(encrypted_name, key)

    def generate_bucket_key(self, mnemonic: str, bucket_id: str) -> str:
        """
        Generate bucket key for metadata operations
        """
        seed = self.mnemonic_gen.to_seed(mnemonic).hex()
        
        # Generate deterministic key
        sha512_input = seed + bucket_id
        deterministic_key = hashlib.sha512(bytes.fromhex(sha512_input)).hexdigest()
        
        return deterministic_key[:64]

    def generate_filename_encryption_key(self, bucket_key: str) -> bytes:
        """Generate encryption key for filename using BUCKET_META_MAGIC"""
        hasher = hmac.new(bytes.fromhex(bucket_key), BUCKET_META_MAGIC, hashlib.sha512)
        return hasher.digest()[:32]

    def generate_filename_encryption_iv(self, bucket_key: str, bucket_id: str, filename: str) -> bytes:
        """Generate encryption IV for filename"""
        hasher = hmac.new(bytes.fromhex(bucket_key), digestmod=hashlib.sha512)
        hasher.update(bucket_id.encode())
        hasher.update(filename.encode())
        return hasher.digest()[:32]

    def encrypt_meta(self, file_meta: str, key: bytes, iv: bytes) -> str:
        """Encrypt metadata using AES-256-GCM"""
        cipher = Cipher(algorithms.AES(key), modes.GCM(iv[:16]), backend=self.backend)
        encryptor = cipher.encryptor()
        
        cipher_text = encryptor.update(file_meta.encode('utf-8')) + encryptor.finalize()
        auth_tag = encryptor.tag
        
        # Concatenate auth_tag + iv + cipher_text and encode to base64
        result = auth_tag + iv + cipher_text
        return base64.b64encode(result).decode('ascii')

    def decrypt_meta(self, buffer_base64: str, decrypt_key: str) -> Optional[str]:
        """Decrypt metadata using AES-256-GCM"""
        try:
            data = base64.b64decode(buffer_base64)

            # Extract components
            GCM_DIGEST_SIZE = 16
            SHA256_DIGEST_SIZE = 32

            digest = data[:GCM_DIGEST_SIZE]
            iv = data[GCM_DIGEST_SIZE:GCM_DIGEST_SIZE + SHA256_DIGEST_SIZE]
            buffer = data[GCM_DIGEST_SIZE + SHA256_DIGEST_SIZE:]

            # Create decipher with auth tag
            key_bytes = bytes.fromhex(decrypt_key)[:32]
            cipher = Cipher(algorithms.AES(key_bytes), modes.GCM(iv[:16], digest), backend=self.backend)
            decryptor = cipher.decryptor()

            decrypted = decryptor.update(buffer) + decryptor.finalize()
            return decrypted.decode('utf-8')

        except Exception:
            return None

    # Configuration encryption methods (unchanged)
    def pass_to_hash(self, password: str, salt: Optional[str] = None) -> dict:
        if salt is None:
            salt_bytes = os.urandom(16)
            salt = salt_bytes.hex()
        else:
            salt_bytes = bytes.fromhex(salt)

        # SHA1 is required here for compatibility with the Internxt server's
        # PBKDF2 password hash format. Do not change without backend changes.
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA1(),  # nosec B303 - protocol compatibility
            length=32,
            salt=salt_bytes,
            iterations=10000,
            backend=self.backend
        )
        hash_bytes = kdf.derive(password.encode('utf-8'))
        return {'salt': salt, 'hash': hash_bytes.hex()}

    def encrypt_text(self, text: str) -> str:
        app_crypto_secret = config_service.get('APP_CRYPTO_SECRET')
        return self.encrypt_text_with_key(text, app_crypto_secret)

    def decrypt_text(self, encrypted_text: str) -> str:
        app_crypto_secret = config_service.get('APP_CRYPTO_SECRET')
        return self.decrypt_text_with_key(encrypted_text, app_crypto_secret)

    def encrypt_text_with_key(self, text_to_encrypt: str, secret: str) -> str:
        salt = os.urandom(8)
        key, iv = self._get_key_and_iv_from(secret, salt)
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=self.backend)
        encryptor = cipher.encryptor()
        
        text_bytes = text_to_encrypt.encode('utf-8')
        padding_length = 16 - (len(text_bytes) % 16)
        padded_text = text_bytes + bytes([padding_length] * padding_length)
        
        encrypted = encryptor.update(padded_text) + encryptor.finalize()
        result = b'Salted__' + salt + encrypted
        return result.hex()

    def decrypt_text_with_key(self, encrypted_text: str, secret: str) -> str:
        cipher_bytes = bytes.fromhex(encrypted_text)
        salt = cipher_bytes[8:16]
        key, iv = self._get_key_and_iv_from(secret, salt)
        
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=self.backend)
        decryptor = cipher.decryptor()
        
        contents_to_decrypt = cipher_bytes[16:]
        decrypted_padded = decryptor.update(contents_to_decrypt) + decryptor.finalize()
        
        padding_length = decrypted_padded[-1]
        decrypted = decrypted_padded[:-padding_length] if padding_length <= 16 else decrypted_padded
        return decrypted.decode('utf-8')

    def _get_key_and_iv_from(self, secret: str, salt: bytes) -> Tuple[bytes, bytes]:
        # MD5 is required here to match OpenSSL's EVP_BytesToKey, which the
        # Internxt CLI uses for credential file encryption. Do not change.
        password = secret.encode('latin-1') + salt
        md5_hashes = []
        digest = password
        for _ in range(3):
            md5_hashes.append(hashlib.md5(digest, usedforsecurity=False).digest())  # nosec B324
            digest = md5_hashes[-1] + password
        key = md5_hashes[0] + md5_hashes[1]
        iv = md5_hashes[2]
        return key, iv

    def validate_mnemonic(self, mnemonic_phrase: str) -> bool:
        return self.mnemonic_gen.check(mnemonic_phrase)

    def encrypt_password_hash(self, password: str, encrypted_salt: str) -> str:
        salt = self.decrypt_text(encrypted_salt)
        hash_obj = self.pass_to_hash(password, salt)
        return self.encrypt_text(hash_obj['hash'])

    def _internxt_aes_gcm_encrypt(self, text: str, password: str,
                                  iv_hex: str, salt_hex: str, hops: int = 2145) -> str:
        """
        Replicates @internxt/lib `aes.encrypt(text, password, {iv, salt})`.

        AES-256-GCM with a PBKDF2-SHA512 derived key (2145 iterations, 32 bytes).
        Output layout (base64-encoded): salt[64] + iv[16] + authTag[16] + ciphertext.
        The fixed iv/salt come from APP_MAGIC_IV / APP_MAGIC_SALT, matching the
        official CLI's KeysService.encryptPrivateKey via CryptoUtils.getAesInit().
        """
        iv = bytes.fromhex(iv_hex)
        salt = bytes.fromhex(salt_hex)
        kdf = PBKDF2HMAC(algorithm=hashes.SHA512(), length=32, salt=salt,
                         iterations=hops, backend=self.backend)
        key = kdf.derive(password.encode('utf-8'))
        ciphertext_with_tag = AESGCM(key).encrypt(iv, text.encode('utf-8'), None)
        ciphertext, tag = ciphertext_with_tag[:-16], ciphertext_with_tag[-16:]
        return base64.b64encode(salt + iv + tag + ciphertext).decode('ascii')

    # --- OpenPGP keypair backends ------------------------------------------
    # Internxt validates the login-payload OpenPGP keys with openpgp.js, so we
    # must ship a genuine EdDSA/Ed25519 primary key + ECDH/Curve25519 encryption
    # subkey ("ed25519Legacy"). Several libraries can produce this; we try them
    # in order and use the first that works, so login keeps working regardless of
    # what's installed or which Python version is in use.

    def _openpgp_keypair_pgpy(self) -> Tuple[str, str]:
        """Preferred backend: PGPy. Unavailable on Python 3.13+ (PGPy imports the
        removed `imghdr` stdlib module), so this raises there and we fall through."""
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import pgpy
            from pgpy.constants import (
                PubKeyAlgorithm, KeyFlags, HashAlgorithm,
                SymmetricKeyAlgorithm, CompressionAlgorithm, EllipticCurveOID
            )
            # Primary signing/certifying key: EdDSA over Ed25519 (legacy format).
            key = pgpy.PGPKey.new(PubKeyAlgorithm.EdDSA, EllipticCurveOID.Ed25519)
            uid = pgpy.PGPUID.new('inxt@inxt.com')
            key.add_uid(
                uid,
                usage={KeyFlags.Sign, KeyFlags.Certify},
                hashes=[HashAlgorithm.SHA256],
                ciphers=[SymmetricKeyAlgorithm.AES256],
                compression=[CompressionAlgorithm.ZLIB],
            )
            # Encryption subkey: ECDH over Curve25519, matching openpgp.js ed25519Legacy.
            subkey = pgpy.PGPKey.new(PubKeyAlgorithm.ECDH, EllipticCurveOID.Curve25519)
            key.add_subkey(
                subkey,
                usage={KeyFlags.EncryptCommunications, KeyFlags.EncryptStorage},
            )
        return str(key.pubkey), str(key)

    def _openpgp_keypair_native(self) -> Tuple[str, str]:
        """Zero-dependency backend: serialise the OpenPGP packets ourselves using
        `cryptography` alone. Works on every supported Python (validated against
        openpgp.js). See services/openpgp_native.py."""
        from services.openpgp_native import generate_ed25519legacy_keypair
        return generate_ed25519legacy_keypair('inxt@inxt.com')

    def _openpgp_keypair_gnupg(self) -> Tuple[str, str]:
        """Fallback backend: shell out to the system GnuPG via python-gnupg."""
        import shutil
        import tempfile
        import gnupg
        if not shutil.which('gpg') and not shutil.which('gpg2'):
            raise RuntimeError("gpg binary not found on PATH")
        home = tempfile.mkdtemp(prefix='inxt-gpg-')
        try:
            gpg = gnupg.GPG(gnupghome=home)
            params = gpg.gen_key_input(
                key_type='eddsa', key_curve='ed25519',
                subkey_type='ecdh', subkey_curve='cv25519',
                name_email='inxt@inxt.com', no_protection=True,
            )
            result = gpg.gen_key(params)
            if not getattr(result, 'fingerprint', None):
                raise RuntimeError(f"gpg key generation failed: {result!r}")
            public = gpg.export_keys(result.fingerprint)
            # GnuPG 2.1+ refuses to export a secret key without a passphrase even
            # when the key is unprotected; an empty loopback passphrase satisfies
            # it and yields the unencrypted armored secret key.
            private = gpg.export_keys(
                result.fingerprint, True,  # secret key
                passphrase='', expect_passphrase=False,
            )
            if not public or not private:
                raise RuntimeError("gpg key export returned empty output")
            return public, private
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def _generate_openpgp_armored_keypair(self) -> Tuple[str, str]:
        """Produce (public_key_armored, private_key_armored), trying each backend
        in preference order. Raises only if every backend fails."""
        backends = (
            ('pgpy', self._openpgp_keypair_pgpy),
            ('native', self._openpgp_keypair_native),
            ('gnupg', self._openpgp_keypair_gnupg),
        )
        errors = []
        for name, backend in backends:
            try:
                public, private = backend()
                if public and private:
                    _dbg(f"   🔑 OpenPGP keys via {name} backend")
                    return public, private
                errors.append(f"{name}: returned empty keys")
            except Exception as e:  # ImportError, missing gpg, generation errors…
                errors.append(f"{name}: {e}")
        raise RuntimeError(
            "Could not generate the OpenPGP login keys with any backend. Install "
            "one of: PGPy (Python <3.13 only), python-gnupg + a gpg binary, or "
            "ensure `cryptography` supports Ed25519/X25519 for the built-in native "
            "backend. Details — " + "; ".join(errors)
        )

    def generate_keys(self, password: str) -> Dict[str, Any]:
        """
        Generates a real OpenPGP Ed25519 (legacy) keypair for the login payload.

        Internxt's server validates these keys ("keys.ecc.publicKey is not a valid
        OpenPGP public key"), so placeholders are rejected. This mirrors the
        official CLI's KeysService.generateNewKeysWithEncrypted: an EdDSA/Ed25519
        primary key plus an ECDH/Curve25519 encryption subkey, with the public key
        sent base64-encoded and the armored private key AES-256-GCM encrypted.

        The keypair itself comes from whichever OpenPGP backend is available
        (PGPy → native → GnuPG); see _generate_openpgp_armored_keypair.

        Fresh keys are generated on every login (as the official SDK does); the
        server preserves any pre-existing account keys, so this is non-destructive.
        """
        public_key_armored, private_key_armored = self._generate_openpgp_armored_keypair()

        public_key_b64 = base64.b64encode(public_key_armored.encode('utf-8')).decode('ascii')
        private_key_encrypted = self._internxt_aes_gcm_encrypt(
            private_key_armored, password,
            config_service.get('APP_MAGIC_IV'), config_service.get('APP_MAGIC_SALT')
        )

        return {
            "privateKeyEncrypted": private_key_encrypted,
            "publicKey": public_key_b64,
            "revocationCertificate": "",
            "ecc": {"publicKey": public_key_b64, "privateKeyEncrypted": private_key_encrypted},
            "kyber": {"publicKey": None, "privateKeyEncrypted": None},
        }

# Global instance
crypto_service = CryptoService()