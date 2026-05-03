"""Tests for NetworkUtils SSL certificate lifecycle.

Generates real self-signed certs in a tmp directory and verifies the full
generate → save → get → validate cycle.
"""
from unittest.mock import patch

import pytest

from services.network_utils import NetworkUtils


@pytest.fixture
def isolated_certs(tmp_path):
    """Point all SSL paths at a tmp dir so no global state is touched."""
    with patch.object(NetworkUtils, 'WEBDAV_SSL_CERTS_DIR', tmp_path), \
         patch.object(NetworkUtils, 'WEBDAV_SSL_CERT_FILE', tmp_path / 'cert.crt'), \
         patch.object(NetworkUtils, 'WEBDAV_SSL_KEY_FILE', tmp_path / 'priv.key'):
        yield tmp_path


# ---------- get_auth_from_credentials ----------

def test_get_auth_hashes_password_with_sha256():
    out = NetworkUtils.get_auth_from_credentials({'user': 'u@ex.com', 'pass': 'pw'})
    import hashlib
    assert out['username'] == 'u@ex.com'
    assert out['password'] == hashlib.sha256('pw'.encode()).hexdigest()


def test_get_auth_handles_missing_fields():
    """Empty creds dict should not raise — returns empty hash."""
    out = NetworkUtils.get_auth_from_credentials({})
    assert out['username'] == ''
    # SHA256 of empty string is well-defined, won't raise
    assert isinstance(out['password'], str)
    assert len(out['password']) == 64


# ---------- generate_new_selfsigned_certs ----------

def test_generate_certs_returns_pem_bytes(isolated_certs):
    result = NetworkUtils.generate_new_selfsigned_certs()
    assert 'cert' in result and 'key' in result
    assert result['cert'].startswith(b'-----BEGIN CERTIFICATE-----')
    assert result['key'].startswith(b'-----BEGIN ')


def test_generate_certs_writes_files_to_disk(isolated_certs):
    NetworkUtils.generate_new_selfsigned_certs()
    assert (isolated_certs / 'cert.crt').exists()
    assert (isolated_certs / 'priv.key').exists()


def test_generated_cert_is_valid_for_at_least_a_day(isolated_certs):
    NetworkUtils.generate_new_selfsigned_certs()
    info = NetworkUtils.validate_ssl_certificates()
    assert info['valid'] is True
    assert info['expired'] is False
    assert info['days_until_expiry'] >= 1


# ---------- save_webdav_ssl_certs ----------

def test_save_writes_files_with_secure_permissions(isolated_certs):
    cert = b'-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n'
    key = b'-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n'
    NetworkUtils.save_webdav_ssl_certs(cert, key)

    cert_file = isolated_certs / 'cert.crt'
    key_file = isolated_certs / 'priv.key'
    assert cert_file.read_bytes() == cert
    assert key_file.read_bytes() == key

    import os
    import stat
    if os.name != 'nt':
        # Owner read-only/write for the private key — never world-readable.
        mode = stat.S_IMODE(key_file.stat().st_mode)
        assert mode == 0o600, f"key file has insecure mode {oct(mode)}"


# ---------- get_webdav_ssl_certs (cached path + regen path) ----------

def test_get_certs_generates_when_missing(isolated_certs):
    """Fresh tmp dir → no certs → must auto-generate."""
    out = NetworkUtils.get_webdav_ssl_certs()
    assert out['cert'].startswith(b'-----BEGIN CERTIFICATE-----')
    assert (isolated_certs / 'cert.crt').exists()


def test_get_certs_reuses_existing_when_valid(isolated_certs):
    """Second call must NOT regenerate — content is byte-identical."""
    a = NetworkUtils.get_webdav_ssl_certs()
    b = NetworkUtils.get_webdav_ssl_certs()
    assert a['cert'] == b['cert']
    assert a['key'] == b['key']


def test_get_certs_regenerates_when_expired(isolated_certs):
    """If validate_ssl_certificates says expired, generate fresh."""
    a = NetworkUtils.get_webdav_ssl_certs()
    # Force the cert file to look like garbage so the validity check fails.
    (isolated_certs / 'cert.crt').write_bytes(b'invalid-pem-content')
    b = NetworkUtils.get_webdav_ssl_certs()
    assert b['cert'].startswith(b'-----BEGIN CERTIFICATE-----')
    assert b['cert'] != a['cert']


# ---------- validate_ssl_certificates ----------

def test_validate_returns_invalid_when_no_files(isolated_certs):
    info = NetworkUtils.validate_ssl_certificates()
    assert info['valid'] is False
    assert 'not found' in info['message'].lower()


def test_validate_returns_invalid_for_garbage_files(isolated_certs):
    (isolated_certs / 'cert.crt').write_bytes(b'not a cert')
    (isolated_certs / 'priv.key').write_bytes(b'not a key')
    info = NetworkUtils.validate_ssl_certificates()
    assert info['valid'] is False


def test_validate_includes_subject_and_issuer(isolated_certs):
    NetworkUtils.generate_new_selfsigned_certs()
    info = NetworkUtils.validate_ssl_certificates()
    assert info.get('subject')
    assert info.get('issuer')
    # Self-signed -> subject == issuer
    assert info['subject'] == info['issuer']
