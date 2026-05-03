"""Tests for InternxtDAVResource.end_write — the WebDAV upload-on-close hook.

Covers the small-file (memory) vs large-file (disk) branches, the
update-existing vs create-new dispatch, and the with_errors=True abort path.
"""
from unittest.mock import patch, MagicMock

import pytest

from services.webdav_provider import (
    InternxtDAVResource,
    StreamingFileUpload,
)


def _resource(path='/Docs/note.txt', metadata=None, provider=None):
    """Build a resource without invoking DAVNonCollection.__init__."""
    r = InternxtDAVResource.__new__(InternxtDAVResource)
    r.path = path
    r.environ = {}
    r.file_metadata = metadata or {}
    r.provider = provider
    r._upload_buffer = None
    return r


@pytest.fixture
def fake_provider():
    p = MagicMock()
    p.preserve_timestamps = False  # default off so we don't try to read mtimes
    return p


# ---------- with_errors=True abort path ----------

def test_end_write_with_errors_cleans_up_buffer():
    """If the WebDAV layer signals errors, buffer must be cleaned up
    and we exit immediately."""
    r = _resource()
    fake_buffer = MagicMock()
    r._upload_buffer = fake_buffer
    r.end_write(with_errors=True)
    fake_buffer.cleanup.assert_called_once()


def test_end_write_with_no_buffer_set_is_noop():
    """If begin_write was never called, end_write must not crash."""
    r = _resource()
    r._upload_buffer = None
    # Should return cleanly without raising
    r.end_write(with_errors=False)


# ---------- small file (memory) → upload_file_to_folder ----------

def test_end_write_small_file_creates_new(tmp_path, fake_provider):
    """Small file at root path → upload_file_to_folder with the right name/type."""
    r = _resource(path='/note.txt', provider=fake_provider)

    # Set up a memory-only buffer with bytes
    buf = StreamingFileUpload()
    buf.write(b"hello webdav")
    r._upload_buffer = buf

    fake_creds = {'user': {'rootFolderId': 'root-uuid'}}
    captured = {}

    def fake_upload(file_path, parent_uuid, plain_name, file_type=None,
                    creation_time=None, modification_time=None):
        captured['file_path'] = file_path
        captured['parent_uuid'] = parent_uuid
        captured['plain_name'] = plain_name
        captured['file_type'] = file_type
        return {'uuid': 'new-uuid'}

    with patch('services.auth.auth_service.get_auth_details',
               return_value=fake_creds), \
         patch('services.drive.drive_service.upload_file_to_folder',
               side_effect=fake_upload):
        r.end_write(with_errors=False)

    assert captured['parent_uuid'] == 'root-uuid'
    assert captured['plain_name'] == 'note'
    assert captured['file_type'] == 'txt'


def test_end_write_small_file_in_subfolder(fake_provider):
    """Non-root path → resolve parent, then upload there."""
    r = _resource(path='/Docs/note.txt', provider=fake_provider)
    buf = StreamingFileUpload()
    buf.write(b"abc")
    r._upload_buffer = buf

    parent_resolved = {'type': 'folder', 'uuid': 'docs-uuid', 'metadata': {}}
    captured = {}

    def fake_upload(file_path, parent_uuid, plain_name, file_type=None, **kw):
        captured['parent_uuid'] = parent_uuid
        return {'uuid': 'x'}

    with patch('services.drive.drive_service.resolve_path',
               return_value=parent_resolved), \
         patch('services.drive.drive_service.upload_file_to_folder',
               side_effect=fake_upload):
        r.end_write(with_errors=False)

    assert captured['parent_uuid'] == 'docs-uuid'


def test_end_write_small_file_extensionless_uses_empty_type(fake_provider):
    r = _resource(path='/README', provider=fake_provider)
    buf = StreamingFileUpload()
    buf.write(b"readme content")
    r._upload_buffer = buf

    captured = {}
    def fake_upload(file_path, parent_uuid, plain_name, file_type=None, **kw):
        captured['plain_name'] = plain_name
        captured['file_type'] = file_type
        return {'uuid': 'x'}

    fake_creds = {'user': {'rootFolderId': 'root-uuid'}}
    with patch('services.auth.auth_service.get_auth_details',
               return_value=fake_creds), \
         patch('services.drive.drive_service.upload_file_to_folder',
               side_effect=fake_upload):
        r.end_write(with_errors=False)

    assert captured['plain_name'] == 'README'
    assert captured['file_type'] == ''


# ---------- update-existing-file branch ----------

def test_end_write_dispatches_to_update_for_existing_file(fake_provider):
    """If file_metadata has a non-pending uuid, route to update_file
    instead of creating a new entry."""
    r = _resource(path='/note.txt', metadata={'uuid': 'existing-real-uuid'},
                  provider=fake_provider)
    buf = StreamingFileUpload()
    buf.write(b"updated content")
    r._upload_buffer = buf

    fake_creds = {'user': {'rootFolderId': 'root-uuid'}}
    captured = {}

    def fake_update(file_uuid, file_path):
        captured['file_uuid'] = file_uuid
        return {'success': True}

    with patch('services.auth.auth_service.get_auth_details',
               return_value=fake_creds), \
         patch('services.drive.drive_service.update_file',
               side_effect=fake_update), \
         patch('services.drive.drive_service.upload_file_to_folder') as mock_upload:
        r.end_write(with_errors=False)

    assert captured['file_uuid'] == 'existing-real-uuid'
    mock_upload.assert_not_called()


def test_end_write_pending_uuid_routes_to_create_not_update(fake_provider):
    """Resources with 'pending-...' uuids are placeholders; must create new."""
    r = _resource(path='/new.txt',
                  metadata={'uuid': 'pending-new.txt'},
                  provider=fake_provider)
    buf = StreamingFileUpload()
    buf.write(b"payload")
    r._upload_buffer = buf

    fake_creds = {'user': {'rootFolderId': 'root-uuid'}}
    with patch('services.auth.auth_service.get_auth_details',
               return_value=fake_creds), \
         patch('services.drive.drive_service.update_file') as mock_update, \
         patch('services.drive.drive_service.upload_file_to_folder',
               return_value={'uuid': 'x'}):
        r.end_write(with_errors=False)

    mock_update.assert_not_called()


# ---------- large file (disk) branch ----------

def test_end_write_large_file_uploads_disk_path(fake_provider):
    """For files that overflowed to disk, upload via the temp file path."""
    r = _resource(path='/big.bin', provider=fake_provider)

    # Build a buffer that's already on disk
    buf = StreamingFileUpload()
    buf._switch_to_disk()
    buf.write(b"X" * 1024)
    r._upload_buffer = buf

    fake_creds = {'user': {'rootFolderId': 'root-uuid'}}
    captured = {}
    def fake_upload(file_path, parent_uuid, plain_name, file_type=None, **kw):
        captured['file_path'] = file_path
        return {'uuid': 'x'}

    with patch('services.auth.auth_service.get_auth_details',
               return_value=fake_creds), \
         patch('services.drive.drive_service.upload_file_to_folder',
               side_effect=fake_upload):
        r.end_write(with_errors=False)

    # We passed the temp file path (not bytes) to upload_file_to_folder
    assert captured['file_path'] == buf.temp_path
    buf.cleanup()


# ---------- exception in upload → DAVError, buffer still cleaned up ----------

def test_end_write_propagates_upload_failure_as_dav_error(fake_provider):
    from wsgidav.dav_error import DAVError

    r = _resource(path='/x.txt', provider=fake_provider)
    buf = StreamingFileUpload()
    buf.write(b"x")
    r._upload_buffer = buf

    fake_creds = {'user': {'rootFolderId': 'root-uuid'}}
    with patch('services.auth.auth_service.get_auth_details',
               return_value=fake_creds), \
         patch('services.drive.drive_service.upload_file_to_folder',
               side_effect=ConnectionError("net down")):
        with pytest.raises(DAVError):
            r.end_write(with_errors=False)

    # Buffer was cleaned up by the finally clause
    assert buf.closed or buf.temp_file is None or buf.temp_file.closed
