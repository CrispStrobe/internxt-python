"""Tests for InternxtDAVResource.get_content (download path) and begin_write."""
from unittest.mock import MagicMock, patch

import pytest

from services.crypto import crypto_service
from services.webdav_provider import (
    InternxtDAVResource,
    StreamingFileUpload,
    MAX_MEMORY_SIZE,
    webdav_api,
)


def _resource(metadata=None, environ=None):
    """Bypass DAVNonCollection.__init__ for unit testing."""
    r = InternxtDAVResource.__new__(InternxtDAVResource)
    r.file_metadata = metadata or {}
    r.path = '/test'
    r.environ = environ or {}
    r._upload_buffer = None
    return r


@pytest.fixture
def fake_user():
    return {
        'user': {
            'email': 'u@example.com',
            'userId': 'user-uuid',
            'bridgeUser': 'u@example.com',
            'bucket': '00' * 12,
            'mnemonic': ('abandon abandon abandon abandon abandon abandon '
                         'abandon abandon abandon abandon abandon about'),
        },
    }


# ---------- get_content: pending file shortcut ----------

def test_get_content_returns_empty_for_pending_resource():
    """Files in 'pending-' state (created via WebDAV PUT but not yet uploaded)
    return empty BytesIO so clients see a 0-byte file."""
    r = _resource({'uuid': 'pending-doc.txt'})
    out = r.get_content()
    assert out.read() == b''


def test_get_content_returns_empty_when_no_uuid():
    r = _resource({})
    out = r.get_content()
    assert out.read() == b''


# ---------- get_content: full download cycle through real crypto ----------

def test_get_content_downloads_and_decrypts(fake_user):
    payload = b"hello webdav download"
    enc, idx_hex = crypto_service.encrypt_stream_internxt_protocol(
        payload, fake_user['user']['mnemonic'], fake_user['user']['bucket'])

    r = _resource({'uuid': 'real-uuid'})
    fake_isolated_api = MagicMock()
    fake_isolated_api.get_file_metadata.return_value = {
        'bucket': fake_user['user']['bucket'],
        'fileId': 'nfid', 'size': len(payload),
    }
    fake_isolated_api.get_download_links.return_value = {
        'shards': [{'url': 'https://download/'}],
        'index': idx_hex,
    }
    fake_isolated_api.download_chunk.return_value = enc

    with patch('services.auth.auth_service.get_auth_details',
               return_value=fake_user), \
         patch.object(webdav_api, '_get_isolated_session',
                      return_value=fake_isolated_api):
        out = r.get_content()

    assert out.read() == payload


def test_get_content_trims_oversized_decrypted_data(fake_user):
    """If decrypt yields more bytes than metadata.size, must trim."""
    payload = b"exactly twelve!"  # 15 bytes
    enc, idx_hex = crypto_service.encrypt_stream_internxt_protocol(
        payload, fake_user['user']['mnemonic'], fake_user['user']['bucket'])

    # Pretend the metadata says only 12 bytes
    r = _resource({'uuid': 'fid'})
    fake_isolated_api = MagicMock()
    fake_isolated_api.get_file_metadata.return_value = {
        'bucket': fake_user['user']['bucket'],
        'fileId': 'nfid', 'size': 12,
    }
    fake_isolated_api.get_download_links.return_value = {
        'shards': [{'url': 'u'}], 'index': idx_hex,
    }
    fake_isolated_api.download_chunk.return_value = enc

    with patch('services.auth.auth_service.get_auth_details',
               return_value=fake_user), \
         patch.object(webdav_api, '_get_isolated_session',
                      return_value=fake_isolated_api):
        out = r.get_content()

    data = out.read()
    assert data == payload[:12]


def test_get_content_returns_error_bytes_on_failure(fake_user):
    """If download fails, get_content returns an error message as BytesIO
    (rather than raising) — so WebDAV clients see a sensible body."""
    r = _resource({'uuid': 'real-uuid'})
    with patch('services.auth.auth_service.get_auth_details',
               return_value=fake_user), \
         patch.object(webdav_api, '_get_isolated_session',
                      side_effect=ConnectionError("net down")):
        out = r.get_content()
    body = out.read()
    assert b'Error' in body or b'error' in body


# ---------- begin_write ----------

def test_begin_write_creates_upload_buffer():
    r = _resource()
    buf = r.begin_write()
    assert isinstance(buf, StreamingFileUpload)
    assert r._upload_buffer is buf
    buf.cleanup()


def test_begin_write_uses_content_length_for_disk_promotion():
    """When Content-Length is large, the buffer should switch to disk eagerly."""
    r = _resource()
    r.environ = {'CONTENT_LENGTH': str(MAX_MEMORY_SIZE + 1)}
    buf = r.begin_write()
    assert buf.using_disk is True
    buf.cleanup()


def test_begin_write_ignores_invalid_content_length():
    """Non-numeric CONTENT_LENGTH must be tolerated, not crash."""
    r = _resource()
    r.environ = {'CONTENT_LENGTH': 'not-a-number'}
    buf = r.begin_write()
    # Should still construct (just without size hint)
    assert buf is not None
    assert buf.using_disk is False
    buf.cleanup()


def test_begin_write_handles_no_environ():
    """begin_write must work even when self.environ is missing or empty."""
    r = _resource()
    r.environ = None  # missing entirely
    # Should not crash — falls through to no-size-hint branch
    buf = r.begin_write()
    assert buf is not None
    buf.cleanup()
