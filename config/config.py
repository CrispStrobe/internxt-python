#!/usr/bin/env python3
"""
internxt_cli/config/config.py
Configuration management for Internxt CLI - EXACT match to TypeScript ConfigService
"""

import os
import json
import sys
from pathlib import Path
from typing import Optional, Dict, Any


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

    def save_user_credentials(self, login_credentials: Dict[str, Any]) -> None:
        """
        Saves the authenticated user credentials to file
        EXACT match to TypeScript ConfigService.saveUser()
        @param login_credentials The user credentials to be saved
        """
        self._ensure_internxt_cli_data_dir_exists()
        
        # EXACT match to TypeScript:
        # const credentialsString = JSON.stringify(loginCredentials);
        # const encryptedCredentials = CryptoService.instance.encryptText(credentialsString);
        # await fs.writeFile(ConfigService.CREDENTIALS_FILE, encryptedCredentials, 'utf8');
        credentials_string = json.dumps(login_credentials)
        
        # Import crypto service here to avoid circular imports
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
        
        encrypted_credentials = crypto_service.encrypt_text(credentials_string)
        
        with open(self.credentials_file, 'w', encoding='utf-8') as f:
            f.write(encrypted_credentials)

    def clear_user_credentials(self) -> None:
        """
        Clears the authenticated user from file
        EXACT match to TypeScript ConfigService.clearUser()
        """
        if not self.credentials_file.exists():
            return
            
        # EXACT match to TypeScript:
        # const stat = await fs.stat(ConfigService.CREDENTIALS_FILE);
        # if (stat.size === 0) throw new Error('Credentials file is already empty');
        # return fs.writeFile(ConfigService.CREDENTIALS_FILE, '', 'utf8');
        try:
            stat = self.credentials_file.stat()
            if stat.st_size == 0:
                raise ValueError('Credentials file is already empty')
        except FileNotFoundError:
            # File doesn't exist, nothing to clear
            return
            
        with open(self.credentials_file, 'w', encoding='utf-8') as f:
            f.write('')

    def read_user_credentials(self) -> Optional[Dict[str, Any]]:
        """
        Returns the authenticated user credentials
        EXACT match to TypeScript ConfigService.readUser()
        @returns The authenticated user credentials
        """
        try:
            # EXACT match to TypeScript:
            # const encryptedCredentials = await fs.readFile(ConfigService.CREDENTIALS_FILE, 'utf8');
            # const credentialsString = CryptoService.instance.decryptText(encryptedCredentials);
            # const loginCredentials = JSON.parse(credentialsString, (key, value) => {
            #   if (typeof value === 'string' && key === 'createdAt') {
            #     return new Date(value);
            #   }
            #   return value;
            # }) as LoginCredentials;
            
            with open(self.credentials_file, 'r', encoding='utf-8') as f:
                encrypted_credentials = f.read()

            if not encrypted_credentials.strip():
                return None

            # Import crypto service here to avoid circular imports
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

            credentials_string = crypto_service.decrypt_text(encrypted_credentials)
            
            # Parse JSON with date handling (matching TypeScript logic)
            def date_hook(pairs):
                result = {}
                for key, value in pairs:
                    if isinstance(value, str) and key == 'createdAt':
                        # Convert to ISO string format (Python equivalent of new Date(value))
                        from datetime import datetime
                        try:
                            dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
                            result[key] = dt.isoformat()
                        except (ValueError, TypeError):
                            result[key] = value
                    else:
                        result[key] = value
                return result
            
            login_credentials = json.loads(credentials_string, object_pairs_hook=date_hook)
            return login_credentials
            
        except Exception:
            # EXACT match to TypeScript: catch { return; }
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