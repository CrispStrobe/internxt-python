#!/usr/bin/env python3
"""
internxt_cli/config/config.py
Configuration management for Internxt CLI - EXACT match to TypeScript ConfigService
"""

import os
import json
import sys
import base64
from pathlib import Path
from typing import Optional, Dict, Any, Tuple

# Credential storage format. The on-disk file is a small JSON envelope:
#   {"fmt": "inxt-cred-v1", "src": "keyring|env|static", "ct": "<aes-ciphertext>"}
# The ciphertext is the credentials JSON encrypted with a *wrapping secret* whose
# location is given by "src":
#   - keyring : a random per-install key kept in the OS keychain (most secure)
#   - env     : a user-supplied key from INTERNXT_CREDENTIALS_KEY (good for CI)
#   - static  : the legacy public app constant (obfuscation only; file is 0600)
# Legacy files (a bare encrypted blob, no envelope) are still read and migrated.
CRED_FMT = "inxt-cred-v1"
KEYRING_SERVICE = "internxt-cli"
KEYRING_KEY = "wrapping-key"
CRED_KEY_ENV = "INTERNXT_CREDENTIALS_KEY"
NO_KEYRING_ENV = "INTERNXT_NO_KEYRING"


class ConfigService:
    """
    Manages local configuration and credential storage
    EXACT match to TypeScript ConfigService
    """

    def __init__(self):
        # EXACT match to TypeScript ConfigService static paths:
        # static readonly INTERNXT_CLI_DATA_DIR = path.join(os.homedir(), '.internxt-cli');
        # static readonly INTERNXT_CLI_LOGS_DIR = path.join(this.INTERNXT_CLI_DATA_DIR, 'logs');
        # static readonly INTERNXT_TMP_DIR = os.tmpdir();
        # static readonly CREDENTIALS_FILE = path.join(this.INTERNXT_CLI_DATA_DIR, '.inxtcli');
        # static readonly DRIVE_SQLITE_FILE = path.join(this.INTERNXT_CLI_DATA_DIR, 'internxt-cli-drive.sqlite');
        # static readonly WEBDAV_SSL_CERTS_DIR = path.join(this.INTERNXT_CLI_DATA_DIR, 'certs');
        # static readonly WEBDAV_CONFIGS_FILE = path.join(this.INTERNXT_CLI_DATA_DIR, 'config.webdav.inxt');
        
        self.home_dir = Path.home()
        self.internxt_cli_data_dir = self.home_dir / '.internxt-cli'
        self.internxt_cli_logs_dir = self.internxt_cli_data_dir / 'logs'
        self.internxt_tmp_dir = Path.cwd() / 'tmp'  # os.tmpdir() equivalent
        self.credentials_file = self.internxt_cli_data_dir / '.inxtcli'
        self.drive_sqlite_file = self.internxt_cli_data_dir / 'internxt-cli-drive.sqlite'
        self.webdav_ssl_certs_dir = self.internxt_cli_data_dir / 'certs'
        self.webdav_configs_file = self.internxt_cli_data_dir / 'config.webdav.inxt'
        self.webdav_pid_file = self.internxt_cli_data_dir / 'webdav.pid'
        
        # EXACT match to TypeScript WebDAV constants:
        # static readonly WEBDAV_LOCAL_URL = 'webdav.local.internxt.com';
        # static readonly WEBDAV_DEFAULT_PORT = '3005';
        # static readonly WEBDAV_DEFAULT_PROTOCOL = 'https';
        # static readonly WEBDAV_DEFAULT_TIMEOUT = 0;
        self.webdav_local_url = 'webdav.local.internxt.com'
        self.webdav_default_port = '3005'
        self.webdav_default_protocol = 'https'
        self.webdav_default_timeout = 0

        self.config = {
            'DRIVE_NEW_API_URL': "https://gateway.internxt.com/drive",
            'NETWORK_URL': 'https://api.internxt.com',
            'APP_CRYPTO_SECRET': '6KYQBP847D4ATSFA',
            'APP_MAGIC_IV': 'd139cb9a2cd17092e79e1861cf9d7023',
            'APP_MAGIC_SALT': '38dce0391b49efba88dbc8c39ebf868f0267eb110bb0012ab27dc52a528d61b1d1ed9d76f400ff58e3240028442b1eab9bb84e111d9dadd997982dbde9dbd25e'
        }

        self._ensure_internxt_cli_data_dir_exists()

    def save_webdav_pid(self, pid: int) -> None:
        """Save WebDAV server PID"""
        self._ensure_internxt_cli_data_dir_exists()
        with open(self.webdav_pid_file, 'w') as f:
            f.write(str(pid))

    def read_webdav_pid(self) -> Optional[int]:
        """Read WebDAV server PID"""
        try:
            with open(self.webdav_pid_file, 'r') as f:
                return int(f.read().strip())
        except Exception:
            return None

    def clear_webdav_pid(self) -> None:
        """Clear WebDAV server PID"""
        try:
            if self.webdav_pid_file.exists():
                self.webdav_pid_file.unlink()
        except Exception:
            pass

    def get(self, key: str) -> str:
        """
        Gets the value from an environment key
        EXACT match to TypeScript ConfigService.get()
        @param key The environment key to retrieve
        @throws {Error} If key is not found in process.env
        @returns The value from the environment variable
        """
        # First try environment variables (as in TypeScript), then fall back to config
        value = os.environ.get(key)
        if value:
            return value
            
        # Fall back to hardcoded config
        if key not in self.config:
            raise ValueError(f"Config key {key} was not found in process.env")
        return self.config[key]

    # ---------------------------------------------------------------- helpers

    def _get_crypto_service(self):
        """Resolve the crypto service across the repo's import layouts."""
        try:
            from ..services.crypto import crypto_service
        except (ImportError, ValueError):
            try:
                from internxt_cli.services.crypto import crypto_service
            except (ImportError, ValueError):
                current_dir = os.path.dirname(os.path.abspath(__file__))
                parent_dir = os.path.dirname(current_dir)
                if parent_dir not in sys.path:
                    sys.path.insert(0, parent_dir)
                from services.crypto import crypto_service
        return crypto_service

    def _keyring(self):
        """Return the `keyring` module if a usable OS backend is present, else None.

        Set INTERNXT_NO_KEYRING=1 to force the file fallback (headless/CI, tests).
        """
        if os.environ.get(NO_KEYRING_ENV, '').lower() in ('1', 'true', 'yes'):
            return None
        try:
            import keyring
            from keyring.backends.fail import Keyring as _FailKeyring
            if isinstance(keyring.get_keyring(), _FailKeyring):
                return None
            return keyring
        except Exception:
            return None

    def _wrapping_secret_for_save(self) -> Tuple[str, str]:
        """Pick where the credential-encryption key lives, best first.

        keyring (OS keychain) → env (INTERNXT_CREDENTIALS_KEY) → static (legacy).
        """
        kr = self._keyring()
        if kr is not None:
            try:
                secret = kr.get_password(KEYRING_SERVICE, KEYRING_KEY)
                if not secret:
                    secret = base64.b64encode(os.urandom(32)).decode('ascii')
                    kr.set_password(KEYRING_SERVICE, KEYRING_KEY, secret)
                return ('keyring', secret)
            except Exception:
                pass  # no usable backend at runtime → fall through
        env_key = os.environ.get(CRED_KEY_ENV)
        if env_key:
            return ('env', env_key)
        return ('static', self.get('APP_CRYPTO_SECRET'))

    def _resolve_wrapping_secret(self, src: Optional[str]) -> Optional[str]:
        """Find the key needed to DECRYPT a stored envelope, by its `src`."""
        if src == 'keyring':
            kr = self._keyring()
            return kr.get_password(KEYRING_SERVICE, KEYRING_KEY) if kr else None
        if src == 'env':
            return os.environ.get(CRED_KEY_ENV)
        if src == 'static':
            return self.get('APP_CRYPTO_SECRET')
        return None

    def _restrict_file_perms(self, path: Path) -> None:
        """Best-effort chmod 600 (no-op where unsupported, e.g. Windows)."""
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _parse_credentials(credentials_string: str) -> Dict[str, Any]:
        def date_hook(pairs):
            result = {}
            for key, value in pairs:
                if isinstance(value, str) and key == 'createdAt':
                    from datetime import datetime
                    try:
                        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
                        result[key] = dt.isoformat()
                    except (ValueError, TypeError):
                        result[key] = value
                else:
                    result[key] = value
            return result
        return json.loads(credentials_string, object_pairs_hook=date_hook)

    # ------------------------------------------------------------- public API

    def save_user_credentials(self, login_credentials: Dict[str, Any]) -> None:
        """Encrypt and persist the authenticated user credentials.

        The credentials JSON is encrypted with a wrapping key sourced from the OS
        keychain when available (random per-install key), else INTERNXT_CREDENTIALS_KEY,
        else the legacy static constant. The file is written 0600 and only ever
        holds ciphertext.
        """
        self._ensure_internxt_cli_data_dir_exists()
        credentials_string = json.dumps(login_credentials)
        crypto_service = self._get_crypto_service()

        src, secret = self._wrapping_secret_for_save()
        ciphertext = crypto_service.encrypt_text_with_key(credentials_string, secret)
        envelope = json.dumps({"fmt": CRED_FMT, "src": src, "ct": ciphertext})

        with open(self.credentials_file, 'w', encoding='utf-8') as f:
            f.write(envelope)
        self._restrict_file_perms(self.credentials_file)

    def clear_user_credentials(self) -> None:
        """Clear the stored credentials (file truncated + keyring key removed)."""
        # Drop the OS-keychain wrapping key too, so nothing decryptable remains.
        kr = self._keyring()
        if kr is not None:
            try:
                kr.delete_password(KEYRING_SERVICE, KEYRING_KEY)
            except Exception:
                pass

        if not self.credentials_file.exists():
            return
        try:
            stat = self.credentials_file.stat()
            if stat.st_size == 0:
                raise ValueError('Credentials file is already empty')
        except FileNotFoundError:
            return

        with open(self.credentials_file, 'w', encoding='utf-8') as f:
            f.write('')

    def read_user_credentials(self) -> Optional[Dict[str, Any]]:
        """Decrypt and return the stored credentials, or None.

        Handles both the JSON envelope (current) and a bare legacy blob, which is
        transparently migrated to the envelope (and the keychain) on read.
        """
        try:
            with open(self.credentials_file, 'r', encoding='utf-8') as f:
                stored = f.read()

            if not stored.strip():
                return None

            crypto_service = self._get_crypto_service()

            # Current format: JSON envelope with an explicit key source.
            if stored.lstrip().startswith('{'):
                try:
                    envelope = json.loads(stored)
                except (ValueError, json.JSONDecodeError):
                    envelope = None
                if isinstance(envelope, dict) and envelope.get('fmt') == CRED_FMT:
                    secret = self._resolve_wrapping_secret(envelope.get('src'))
                    if not secret:
                        return None  # wrapping key unavailable (e.g. keychain gone)
                    plain = crypto_service.decrypt_text_with_key(envelope['ct'], secret)
                    return self._parse_credentials(plain)

            # Legacy format: a bare static-key blob. Decrypt, then migrate.
            plain = crypto_service.decrypt_text(stored)
            credentials = self._parse_credentials(plain)
            try:
                self.save_user_credentials(credentials)  # upgrade to envelope/keychain
            except Exception:
                pass
            return credentials

        except Exception:
            return None

    def save_webdav_config(self, webdav_config: Dict[str, Any]) -> None:
        """
        Save WebDAV configuration
        EXACT match to TypeScript ConfigService.saveWebdavConfig()
        """
        self._ensure_internxt_cli_data_dir_exists()
        
        configs = json.dumps(webdav_config, indent=2)  # Added indent for readability
        
        with open(self.webdav_configs_file, 'w', encoding='utf-8') as f:
            f.write(configs)

    def read_webdav_config(self) -> Dict[str, Any]:
        """
        Read WebDAV configuration with defaults
        Enhanced with timestamp preservation support
        """
        try:
            with open(self.webdav_configs_file, 'r', encoding='utf-8') as f:
                configs_data = f.read()

            configs = json.loads(configs_data)

            return {
                'host': configs.get('host', '127.0.0.1'),  # loopback by default
                'port': configs.get('port', self.webdav_default_port),
                'protocol': configs.get('protocol', self.webdav_default_protocol),
                'timeoutMinutes': configs.get('timeoutMinutes', self.webdav_default_timeout),
                'preserveTimestamps': configs.get('preserveTimestamps', True),
            }

        except Exception:
            # Return default config with timestamp preservation enabled
            return {
                'host': '127.0.0.1',
                'port': self.webdav_default_port,
                'protocol': self.webdav_default_protocol,
                'timeoutMinutes': self.webdav_default_timeout,
                'preserveTimestamps': True,
            }

    def _ensure_internxt_cli_data_dir_exists(self) -> None:
        """
        Ensure configuration directory exists
        EXACT match to TypeScript ConfigService.ensureInternxtCliDataDirExists()
        """
        # EXACT match to TypeScript:
        # try {
        #   await fs.access(ConfigService.INTERNXT_CLI_DATA_DIR);
        # } catch {
        #   await fs.mkdir(ConfigService.INTERNXT_CLI_DATA_DIR);
        # }
        try:
            # Check if directory exists (equivalent to fs.access)
            if not self.internxt_cli_data_dir.exists():
                raise FileNotFoundError()
        except FileNotFoundError:
            self.internxt_cli_data_dir.mkdir(parents=True, exist_ok=True)
        # Keep the data dir private (credentials live here). Best-effort.
        try:
            os.chmod(self.internxt_cli_data_dir, 0o700)
        except OSError:
            pass

    def ensure_webdav_certs_dir_exists(self) -> None:
        """
        Ensure WebDAV certs directory exists
        EXACT match to TypeScript ConfigService.ensureWebdavCertsDirExists()
        """
        try:
            if not self.webdav_ssl_certs_dir.exists():
                raise FileNotFoundError()
        except FileNotFoundError:
            self.webdav_ssl_certs_dir.mkdir(parents=True, exist_ok=True)

    def ensure_internxt_logs_dir_exists(self) -> None:
        """
        Ensure logs directory exists  
        EXACT match to TypeScript ConfigService.ensureInternxtLogsDirExists()
        """
        try:
            if not self.internxt_cli_logs_dir.exists():
                raise FileNotFoundError()
        except FileNotFoundError:
            self.internxt_cli_logs_dir.mkdir(parents=True, exist_ok=True)


# Global instance - EXACT match to TypeScript: export const config_service = ConfigService()
config_service = ConfigService()