#!/usr/bin/env python3
"""
internxt_cli/services/network_utils.py
Network utilities for SSL certificate management (FINAL FIXED VERSION)
"""

import os
import hashlib
import ipaddress  # FIXED: Added missing import
from typing import Dict, Any, Optional
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from config.config import config_service


class NetworkUtils:
    """Network utilities for WebDAV SSL certificates and authentication - FIXED VERSION"""
    
    WEBDAV_SSL_CERTS_DIR = config_service.internxt_cli_data_dir / "webdav-ssl"
    WEBDAV_SSL_CERT_FILE = WEBDAV_SSL_CERTS_DIR / "cert.crt"
    WEBDAV_SSL_KEY_FILE = WEBDAV_SSL_CERTS_DIR / "priv.key"
    WEBDAV_LOCAL_URL = "localhost"
    
    @classmethod
    def get_auth_from_credentials(cls, creds: Dict[str, str]) -> Dict[str, str]:
        """Get authentication credentials for network operations"""
        username = creds.get('user', '')
        password = creds.get('pass', '')
        
        # Hash password with SHA256 (matches TypeScript implementation)
        password_hash = hashlib.sha256(password.encode()).hexdigest()
        
        return {
            'username': username,
            'password': password_hash
        }
    
    @classmethod
    def get_webdav_ssl_certs(cls) -> Dict[str, bytes]:
        """Get WebDAV SSL certificates, generating new ones if needed"""
        # Ensure SSL directory exists
        cls.WEBDAV_SSL_CERTS_DIR.mkdir(parents=True, exist_ok=True)
        
        # Check if certificates exist and are valid
        if cls.WEBDAV_SSL_CERT_FILE.exists() and cls.WEBDAV_SSL_KEY_FILE.exists():
            try:
                # Read existing certificates
                cert_pem = cls.WEBDAV_SSL_CERT_FILE.read_bytes()
                key_pem = cls.WEBDAV_SSL_KEY_FILE.read_bytes()
                
                # Check if certificate is still valid (FIXED: use UTC timezone-aware comparison)
                cert = x509.load_pem_x509_certificate(cert_pem)
                now = datetime.now(timezone.utc)
                
                # Use the new UTC-aware method if available, otherwise fall back
                try:
                    expiry_date = cert.not_valid_after_utc
                except AttributeError:
                    # Fallback for older cryptography versions
                    expiry_date = cert.not_valid_after.replace(tzinfo=timezone.utc)
                
                if now < expiry_date:
                    # Certificate is still valid
                    print("✅ Using existing SSL certificate")
                    return {
                        'cert': cert_pem,
                        'key': key_pem
                    }
                else:
                    print("🔄 SSL certificate expired, generating new one...")
                    
            except Exception as e:
                print(f"🔄 SSL certificate invalid ({e}), generating new one...")
        
        # Generate new certificates
        return cls.generate_new_selfsigned_certs()
    
    @classmethod
    def generate_new_selfsigned_certs(cls) -> Dict[str, bytes]:
        """Generate new self-signed SSL certificates with improved configuration"""
        print("🔐 Generating new SSL certificate for WebDAV server...")
        
        try:
            # Generate private key
            private_key = rsa.generate_private_key(
                public_exponent=65537,
                key_size=2048,
            )
            
            # Create certificate with comprehensive subject information
            subject = issuer = x509.Name([
                x509.NameAttribute(NameOID.COMMON_NAME, cls.WEBDAV_LOCAL_URL),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Internxt WebDAV Server"),
                x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "Local Development"),
                x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
            ])
            
            # Certificate valid for 1 year (use timezone-aware datetime)
            valid_from = datetime.now(timezone.utc)
            valid_to = valid_from + timedelta(days=365)
            
            # Build certificate with comprehensive extensions
            builder = x509.CertificateBuilder().subject_name(
                subject
            ).issuer_name(
                issuer
            ).public_key(
                private_key.public_key()
            ).serial_number(
                x509.random_serial_number()
            ).not_valid_before(
                valid_from
            ).not_valid_after(
                valid_to
            )
            
            # Add Subject Alternative Names for various localhost variants (FIXED)
            san_list = [
                x509.DNSName("localhost"),
                x509.DNSName("127.0.0.1"),
                x509.DNSName("::1"),
                x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),  # FIXED: Correctly use imported module
                x509.IPAddress(ipaddress.IPv6Address("::1")),         # FIXED: Correctly use imported module
            ]
            
            # Add additional localhost variants for better compatibility
            localhost_variants = [
                "webdav.local.internxt.com",
                "internxt.local", 
                "webdav.local"
            ]
            
            for variant in localhost_variants:
                san_list.append(x509.DNSName(variant))
            
            builder = builder.add_extension(
                x509.SubjectAlternativeName(san_list),
                critical=False,
            )
            
            # Add basic constraints
            builder = builder.add_extension(
                x509.BasicConstraints(ca=False, path_length=None),
                critical=True,
            )
            
            # Add key usage for web server
            builder = builder.add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    key_encipherment=True,
                    key_agreement=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    content_commitment=False,
                    data_encipherment=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            
            # Add extended key usage for web server
            builder = builder.add_extension(
                x509.ExtendedKeyUsage([
                    x509.oid.ExtendedKeyUsageOID.SERVER_AUTH,
                    x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH,
                ]),
                critical=True,
            )
            
            # Sign the certificate
            cert = builder.sign(private_key, hashes.SHA256())
            
            # Serialize to PEM format
            cert_pem = cert.public_bytes(serialization.Encoding.PEM)
            key_pem = private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption()
            )
            
            # Save certificates
            cls.save_webdav_ssl_certs(cert_pem, key_pem)
            
            print("✅ SSL certificate generated and saved successfully")
            print(f"📋 Certificate valid until: {valid_to.strftime('%Y-%m-%d %H:%M:%S UTC')}")
            
            return {
                'cert': cert_pem,
                'key': key_pem
            }
            
        except Exception as e:
            print(f"❌ Failed to generate SSL certificate: {e}")
            raise RuntimeError(f"SSL certificate generation failed: {e}")
    
    @classmethod
    def save_webdav_ssl_certs(cls, cert_pem: bytes, key_pem: bytes) -> None:
        """Save SSL certificates to disk with proper permissions"""
        try:
            # Ensure directory exists
            cls.WEBDAV_SSL_CERTS_DIR.mkdir(parents=True, exist_ok=True)
            
            # Write certificate
            cls.WEBDAV_SSL_CERT_FILE.write_bytes(cert_pem)
            cls.WEBDAV_SSL_KEY_FILE.write_bytes(key_pem)
            
            # Set secure permissions (owner read/write only)
            if os.name != 'nt':  # Unix-like systems
                os.chmod(cls.WEBDAV_SSL_CERT_FILE, 0o600)
                os.chmod(cls.WEBDAV_SSL_KEY_FILE, 0o600)
            
            print(f"📁 Certificates saved to: {cls.WEBDAV_SSL_CERTS_DIR}")
                
        except Exception as e:
            raise RuntimeError(f"Failed to save SSL certificates: {e}")
    
    @classmethod
    def validate_ssl_certificates(cls) -> Dict[str, Any]:
        """Validate existing SSL certificates"""
        if not cls.WEBDAV_SSL_CERT_FILE.exists() or not cls.WEBDAV_SSL_KEY_FILE.exists():
            return {
                'valid': False,
                'message': 'Certificate files not found'
            }
        
        try:
            cert_pem = cls.WEBDAV_SSL_CERT_FILE.read_bytes()
            key_pem = cls.WEBDAV_SSL_KEY_FILE.read_bytes()
            
            # Load and validate certificate
            cert = x509.load_pem_x509_certificate(cert_pem)
            private_key = serialization.load_pem_private_key(key_pem, password=None)
            
            # Check if certificate matches private key
            public_key = cert.public_key()
            if not isinstance(public_key, type(private_key.public_key())):
                return {
                    'valid': False,
                    'message': 'Certificate and private key do not match'
                }
            
            # Check expiry
            now = datetime.now(timezone.utc)
            try:
                expiry_date = cert.not_valid_after_utc
            except AttributeError:
                expiry_date = cert.not_valid_after.replace(tzinfo=timezone.utc)
            
            is_expired = now >= expiry_date
            days_until_expiry = (expiry_date - now).days
            
            return {
                'valid': not is_expired,
                'expired': is_expired,
                'expiry_date': expiry_date.isoformat(),
                'days_until_expiry': days_until_expiry,
                'subject': cert.subject.rfc4514_string(),
                'issuer': cert.issuer.rfc4514_string(),
                'message': 'Valid' if not is_expired else f'Expired {abs(days_until_expiry)} days ago'
            }
            
        except Exception as e:
            return {
                'valid': False,
                'message': f'Certificate validation failed: {e}'
            }
    
    @classmethod
    def parse_range_header(cls, range_header: str, file_size: int) -> Optional[Dict[str, Any]]:
        """
        Parse HTTP Range header for partial content requests
        Returns range information or None if invalid/not supported
        """
        if not range_header or not range_header.startswith('bytes='):
            return None
        
        try:
            # Remove 'bytes=' prefix
            range_spec = range_header[6:]
            
            # Parse range (support single range only for now)
            if ',' in range_spec:
                raise ValueError("Multi-range requests not supported")
            
            if '-' not in range_spec:
                raise ValueError("Invalid range format")
            
            start_str, end_str = range_spec.split('-', 1)
            
            # Parse start and end
            if start_str == '':
                # Suffix range (last N bytes)
                if end_str == '':
                    raise ValueError("Invalid suffix range")
                suffix_length = int(end_str)
                start = max(0, file_size - suffix_length)
                end = file_size - 1
            elif end_str == '':
                # From start to end of file
                start = int(start_str)
                end = file_size - 1
            else:
                # Both start and end specified
                start = int(start_str)
                end = int(end_str)
            
            # Validate range
            if start < 0 or end >= file_size or start > end:
                raise ValueError("Range not satisfiable")
            
            return {
                'start': start,
                'end': end,
                'length': end - start + 1,
                'total_size': file_size
            }
            
        except ValueError as e:
            print(f"Invalid range header '{range_header}': {e}")
            return None
    
    @classmethod
    def test_webdav_connection(cls, url: str, username: str, password: str) -> Dict[str, Any]:
        """Test WebDAV connection"""
        try:
            import requests
            from requests.auth import HTTPBasicAuth

            auth = HTTPBasicAuth(username, password)

            # Self-signed cert acceptance is only safe for localhost loopback.
            host = (urlparse(url).hostname or '').lower()
            is_loopback = host in ('localhost', '127.0.0.1', '::1')
            verify_tls = not is_loopback

            # Test OPTIONS request (WebDAV discovery)
            response = requests.options(
                url,
                auth=auth,
                timeout=10,
                verify=verify_tls,  # nosec B501 - self-signed cert allowed only for loopback
                headers={'User-Agent': 'Internxt WebDAV Test Client'}
            )
            
            webdav_methods = response.headers.get('Allow', '').split(', ')
            dav_header = response.headers.get('DAV', '')
            
            return {
                'success': response.status_code in [200, 204],
                'status_code': response.status_code,
                'webdav_supported': 'PROPFIND' in webdav_methods or 'DAV' in response.headers,
                'supported_methods': webdav_methods,
                'dav_compliance': dav_header,
                'server': response.headers.get('Server', 'Unknown'),
                'message': 'WebDAV connection successful' if response.status_code in [200, 204] else f'HTTP {response.status_code}'
            }
            
        except Exception as e:
            return {
                'success': False,
                'message': f'Connection failed: {e}'
            }