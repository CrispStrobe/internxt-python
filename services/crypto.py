#!/usr/bin/env python3
"""
internxt_cli/services/crypto.py
Cryptographic operations for Internxt CLI
"""
import os
import hashlib
import base64
import hmac
from typing import Tuple, Dict, Any, Optional
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.backends import default_backend
from mnemonic import Mnemonic

from config.config import config_service

# constants from inxt-js crypto.ts
BUCKET_META_MAGIC = bytes([
    66, 150, 71, 16, 50, 114, 88, 160, 163, 35, 154, 65, 162, 213, 226, 215,
    70, 138, 57, 61, 52, 19, 210, 170, 38, 164, 162, 200, 86, 201, 2, 81
])

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

    def generate_keys(self, password: str) -> Dict[str, Any]:
        print("   ⚠️  Generating placeholder PGP keys for login payload.")
        encrypted_pk = self.encrypt_text_with_key("placeholder-private-key-for-login", password)
        return {
            "privateKeyEncrypted": encrypted_pk,
            "publicKey": "placeholder-public-key-for-login",
            "revocationCertificate": "placeholder-revocation-cert-for-login",
            "ecc": { "publicKey": "placeholder-ecc-public-key", "privateKeyEncrypted": encrypted_pk },
            "kyber": { "publicKey": None, "privateKeyEncrypted": None }
        }

# Global instance
crypto_service = CryptoService()