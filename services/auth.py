#!/usr/bin/env python3
"""
internxt_cli/services/auth.py
Authentication service for Internxt CLI
"""
import base64
import hashlib
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from config.config import config_service
from utils.api import api_client
from services.crypto import crypto_service

class AuthService:
    def __init__(self):
        self.config = config_service
        self.api = api_client
        self.crypto = crypto_service
    
    def is_2fa_needed(self, email: str) -> bool:
        try:
            details = self.api.security_details(email)
            is_needed = details.get('tfa', False)
            print(f"    ✅ Security details successful, 2FA Enabled: {is_needed}")
            return is_needed
        except Exception as e:
            print(f"    ⚠️  Could not determine 2FA status. Reason: {e}")
            return False

    def compute_bridge_auth(self, bridge_user: str, user_id: str) -> tuple:
        """
        Creates the Basic Auth tuple for the Internxt Network/Bridge API.
        The password is the SHA256 hash of the UserID.
        """
        print(f"🔐 DEBUG: Computing Bridge Auth for UserID: {user_id}")
        hashed_pass = hashlib.sha256(str(user_id).encode()).hexdigest()
        # Returns tuple for requests (user, pass)
        return (bridge_user, hashed_pass)

    def do_login(self, email: str, password: str, tfa_code: Optional[str] = None) -> Dict[str, Any]:
        """
        Performs the modern Hydrated Login Flow.
        """
        clean_email = email.lower().strip()
        print(f"🚀 TRACE: Starting Hydrated Login for {clean_email}")
        
        # Step 1: Security Details (SDK: securityDetails)
        # Returns the encryption salt (sKey)
        sec_details = self.api.security_details(clean_email)
        s_key = sec_details.get('sKey')
        if not s_key:
            raise ValueError(f"Login failed: Salt (sKey) missing. Response: {sec_details}")

        # Step 2: Client-side Crypto
        encrypted_password_hash = self.crypto.encrypt_password_hash(password, s_key)
        keys_payload = self.crypto.generate_keys(password)

        # Step 3: Access Call (SDK: loginAccess)
        # Sends the encrypted hash and the public/private key pairs
        login_payload = {
            'email': clean_email,
            'password': encrypted_password_hash,
            'tfa': tfa_code,
            'keys': {
                'ecc': {
                    'publicKey': keys_payload['ecc']['publicKey'],
                    'privateKey': keys_payload['ecc']['privateKeyEncrypted']
                }
            },
            'privateKey': keys_payload['privateKeyEncrypted'],
            'publicKey': keys_payload['publicKey']
        }
        
        print("🔐 TRACE: Requesting initial session token...")
        access_res = self.api.login_access(login_payload)
        temp_token = access_res.get('newToken')

        # Step 4: HYDRATION (Mandatory Refresh)
        # Modern Internxt requires calling /refresh to populate storage cluster metadata
        print("💧 TRACE: Hydrating session metadata (bucket, userId, bridgeUser)...")
        hydrated = self.api.refresh_token(temp_token)
        
        user_data = hydrated['user']
        
        # Compute Bridge credentials for Object Storage auth
        import hashlib
        bridge_pass = hashlib.sha256(str(user_data['userId']).encode()).hexdigest()
        
        # Final construction including the decrypted mnemonic
        return {
            'user': {
                **user_data,
                'mnemonic': self.crypto.decrypt_text_with_key(user_data['mnemonic'], password),
                'bridgePass': bridge_pass
            },
            'token': hydrated['token'],
            'newToken': hydrated['newToken'],
            'lastLoggedInAt': datetime.now(timezone.utc).isoformat()
        }

    def refresh_tokens(self) -> Dict[str, Any]:
        """Rotates tokens and updates bridge auth if user metadata changed."""
        creds = self.config.read_user_credentials()
        print(f"🔄 DEBUG: Refreshing tokens for {creds['user']['email']}")
        
        try:
            resp = self.api.refresh_token(creds['newToken'])
            
            # Update tokens and bridge auth
            creds['token'] = resp['token']
            creds['newToken'] = resp['newToken']
            creds['user']['bridgeAuth'] = self.compute_bridge_auth(
                resp['user']['bridgeUser'], resp['user']['userId']
            )
            
            self.config.save_user_credentials(creds)
            self.api.set_auth_tokens(creds['token'], creds['newToken'])
            return creds
        except Exception as e:
            print(f"❌ DEBUG: Token rotation failed: {e}")
            raise

    def login(self, email: str, password: str, tfa_code: Optional[str] = None) -> Dict[str, Any]:
        credentials = self.do_login(email, password, tfa_code)
        self.config.save_user_credentials(credentials)
        self.api.set_auth_tokens(credentials.get('token'), credentials.get('newToken'))
        return credentials

    def get_auth_details(self) -> Dict[str, Any]:
        login_creds = self.config.read_user_credentials()
        if not login_creds or not all(k in login_creds for k in ['newToken', 'token']) or not login_creds.get('user', {}).get('mnemonic'):
            raise ValueError("MissingCredentialsError: No valid credentials found. Please login.")
        self.api.set_auth_tokens(login_creds.get('token'), login_creds.get('newToken'))
        return login_creds

    def logout(self) -> None:
        self.config.clear_user_credentials()
        self.api.set_auth_tokens(None, None)
        print("    ✅ Local credentials cleared.")

    def whoami(self) -> Optional[Dict[str, Any]]:
        try:
            credentials = self.get_auth_details()
            user = credentials.get('user', {})
            return {
                'email': user.get('email', ''), 'uuid': user.get('uuid', ''),
                'rootFolderId': user.get('rootFolderId', user.get('root_folder_id', '')),
            }
        except ValueError:
            return None

auth_service = AuthService()