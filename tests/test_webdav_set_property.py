"""Tests for set_property (PROPPATCH timestamps) on both files and folders.

WebDAV clients on macOS/Windows and rclone --metadata use PROPPATCH to set
creation/modification times. Our impl translates these into drive_service
calls for both InternxtDAVCollection (folders) and InternxtDAVResource (files).
"""
from unittest.mock import patch

import pytest

from services.webdav_provider import InternxtDAVCollection, InternxtDAVResource


class _FakeProvider:
    pass


def _collection(metadata=None, path='/Folder'):
    c = InternxtDAVCollection.__new__(InternxtDAVCollection)
    c.path = path
    c.environ = {'wsgidav.provider': _FakeProvider()}
    c.folder_metadata = metadata or {'uuid': 'fold-uuid'}
    c.provider = None
    c._content_cache = None
    c._content_cached_time = 0.0
    c.CACHE_TIMEOUT = 300
    return c


# ---------- creationdate (DAV namespace) ----------

def test_set_property_dav_creationdate_calls_drive_service():
    c = _collection()
    with patch('services.drive.drive_service.set_folder_timestamps') as mock_set:
        c.set_property('{DAV:}creationdate', '2025-01-01T00:00:00Z')
    mock_set.assert_called_once()
    _, kwargs = mock_set.call_args
    args, _ = mock_set.call_args
    assert args[0] == 'fold-uuid'
    # creation_time gets normalized to ISO with timezone
    assert '2025-01-01' in kwargs['creation_time']


def test_set_property_microsoft_creationdate_alias():
    """The Win32 namespace alias for creationdate must also work."""
    c = _collection()
    with patch('services.drive.drive_service.set_folder_timestamps') as mock_set:
        c.set_property('{urn:schemas-microsoft-com:}creationdate',
                       '2025-01-01T00:00:00Z')
    mock_set.assert_called_once()


def test_set_property_creationdate_invalid_raises_dav_error():
    from wsgidav.dav_error import DAVError
    c = _collection()
    with pytest.raises(DAVError):
        c.set_property('{DAV:}creationdate', 'not-a-real-date')


# ---------- getlastmodified ----------

def test_set_property_getlastmodified_with_rfc1123():
    """RFC 1123 format: 'Wed, 21 Oct 2015 07:28:00 GMT'."""
    c = _collection()
    with patch('services.drive.drive_service.set_folder_timestamps') as mock_set:
        c.set_property('{DAV:}getlastmodified',
                       'Wed, 21 Oct 2015 07:28:00 GMT')
    mock_set.assert_called_once()
    _, kwargs = mock_set.call_args
    assert '2015-10-21' in kwargs['modification_time']


def test_set_property_getlastmodified_with_rfc3339():
    """RFC 3339 format: '2015-10-21T07:28:00Z'."""
    c = _collection()
    with patch('services.drive.drive_service.set_folder_timestamps') as mock_set:
        c.set_property('{DAV:}getlastmodified', '2015-10-21T07:28:00Z')
    mock_set.assert_called_once()


def test_set_property_win32_lastmodified_alias():
    c = _collection()
    with patch('services.drive.drive_service.set_folder_timestamps') as mock_set:
        c.set_property('{urn:schemas-microsoft-com:}Win32LastModifiedTime',
                       'Wed, 21 Oct 2015 07:28:00 GMT')
    mock_set.assert_called_once()


def test_set_property_getlastmodified_garbage_raises_dav_error():
    from wsgidav.dav_error import DAVError
    c = _collection()
    with pytest.raises(DAVError):
        c.set_property('{DAV:}getlastmodified', 'not-a-date')


# ---------- unknown property ----------

def test_set_property_unknown_falls_through_to_super():
    """Unknown properties must defer to the wsgidav default (which currently
    no-ops gracefully)."""
    c = _collection()
    # Just verify it doesn't raise and doesn't call our drive_service.
    with patch('services.drive.drive_service.set_folder_timestamps') as mock_set:
        try:
            c.set_property('{custom:}some-prop', 'value')
        except Exception:
            # super().set_property may also raise — that's fine, we just
            # care that we didn't try to update timestamps.
            pass
    mock_set.assert_not_called()


# ---------- drive_service.set_folder_timestamps (the underlying call) ----------

def test_drive_set_folder_timestamps_sends_update_metadata():
    from services.drive import drive_service
    with patch.object(drive_service.api, 'update_folder_metadata',
                      return_value={'ok': True}) as mock_update, \
         patch.object(drive_service, '_clear_parent_cache_for_item'):
        result = drive_service.set_folder_timestamps(
            'fold-uuid',
            creation_time='2025-01-01T00:00:00Z',
            modification_time='2025-06-01T00:00:00Z',
        )
    args, _ = mock_update.call_args
    assert args[0] == 'fold-uuid'
    assert args[1]['creationTime'] == '2025-01-01T00:00:00Z'
    assert args[1]['modificationTime'] == '2025-06-01T00:00:00Z'
    assert result == {'ok': True}


def test_drive_set_folder_timestamps_only_creation():
    from services.drive import drive_service
    with patch.object(drive_service.api, 'update_folder_metadata',
                      return_value={}) as mock_update, \
         patch.object(drive_service, '_clear_parent_cache_for_item'):
        drive_service.set_folder_timestamps(
            'fold-uuid', creation_time='2025-01-01T00:00:00Z')
    args, _ = mock_update.call_args
    assert 'creationTime' in args[1]
    assert 'modificationTime' not in args[1]


def test_drive_set_folder_timestamps_requires_at_least_one():
    from services.drive import drive_service
    with pytest.raises(ValueError):
        drive_service.set_folder_timestamps('fold-uuid')


def test_drive_set_folder_timestamps_clears_parent_cache():
    from services.drive import drive_service
    with patch.object(drive_service.api, 'update_folder_metadata', return_value={}), \
         patch.object(drive_service, '_clear_parent_cache_for_item') as mock_clear:
        drive_service.set_folder_timestamps('fold-uuid', creation_time='2025-01-01T00:00:00Z')
    mock_clear.assert_called_once_with('fold-uuid', 'folder')


# ==========================================================================
# InternxtDAVResource.set_property (FILE PROPPATCH — Issue #1b)
# ==========================================================================

def _resource(metadata=None, path='/test.txt'):
    r = InternxtDAVResource.__new__(InternxtDAVResource)
    r.path = path
    r.environ = {'wsgidav.provider': _FakeProvider()}
    r.file_metadata = metadata or {'uuid': 'file-uuid'}
    r.provider = None
    r._upload_buffer = None
    return r


# ---------- file creationdate ----------

def test_file_set_property_dav_creationdate():
    r = _resource()
    with patch('services.drive.drive_service.set_file_timestamps') as mock_set:
        r.set_property('{DAV:}creationdate', '2025-01-01T00:00:00Z')
    mock_set.assert_called_once()
    args, kwargs = mock_set.call_args
    assert args[0] == 'file-uuid'
    assert '2025-01-01' in kwargs['creation_time']


def test_file_set_property_microsoft_creationdate():
    r = _resource()
    with patch('services.drive.drive_service.set_file_timestamps') as mock_set:
        r.set_property('{urn:schemas-microsoft-com:}creationdate',
                       '2025-01-01T00:00:00Z')
    mock_set.assert_called_once()


def test_file_set_property_creationdate_invalid_raises_dav_error():
    from wsgidav.dav_error import DAVError
    r = _resource()
    with pytest.raises(DAVError):
        r.set_property('{DAV:}creationdate', 'not-a-date')


# ---------- file getlastmodified ----------

def test_file_set_property_getlastmodified_rfc1123():
    r = _resource()
    with patch('services.drive.drive_service.set_file_timestamps') as mock_set:
        r.set_property('{DAV:}getlastmodified',
                       'Wed, 21 Oct 2015 07:28:00 GMT')
    mock_set.assert_called_once()
    _, kwargs = mock_set.call_args
    assert '2015-10-21' in kwargs['modification_time']


def test_file_set_property_getlastmodified_rfc3339():
    r = _resource()
    with patch('services.drive.drive_service.set_file_timestamps') as mock_set:
        r.set_property('{DAV:}getlastmodified', '2015-10-21T07:28:00Z')
    mock_set.assert_called_once()


def test_file_set_property_win32_lastmodified():
    r = _resource()
    with patch('services.drive.drive_service.set_file_timestamps') as mock_set:
        r.set_property('{urn:schemas-microsoft-com:}Win32LastModifiedTime',
                       'Wed, 21 Oct 2015 07:28:00 GMT')
    mock_set.assert_called_once()


def test_file_set_property_getlastmodified_garbage_raises_dav_error():
    from wsgidav.dav_error import DAVError
    r = _resource()
    with pytest.raises(DAVError):
        r.set_property('{DAV:}getlastmodified', 'not-a-date')


# ---------- file unknown property ----------

def test_file_set_property_unknown_falls_through():
    r = _resource()
    with patch('services.drive.drive_service.set_file_timestamps') as mock_set:
        try:
            r.set_property('{custom:}some-prop', 'value')
        except Exception:
            pass
    mock_set.assert_not_called()


# ---------- drive_service.set_file_timestamps ----------

def test_drive_set_file_timestamps_sends_update_metadata():
    from services.drive import drive_service
    with patch.object(drive_service.api, 'update_file_metadata',
                      return_value={'ok': True}) as mock_update, \
         patch.object(drive_service, '_clear_parent_cache_for_item'):
        result = drive_service.set_file_timestamps(
            'file-uuid',
            creation_time='2025-01-01T00:00:00Z',
            modification_time='2025-06-01T00:00:00Z',
        )
    args, _ = mock_update.call_args
    assert args[0] == 'file-uuid'
    assert args[1]['creationTime'] == '2025-01-01T00:00:00Z'
    assert args[1]['modificationTime'] == '2025-06-01T00:00:00Z'
    assert result == {'ok': True}


def test_drive_set_file_timestamps_requires_at_least_one():
    from services.drive import drive_service
    with pytest.raises(ValueError):
        drive_service.set_file_timestamps('file-uuid')


def test_drive_set_file_timestamps_clears_parent_cache():
    from services.drive import drive_service
    with patch.object(drive_service.api, 'update_file_metadata', return_value={}), \
         patch.object(drive_service, '_clear_parent_cache_for_item') as mock_clear:
        drive_service.set_file_timestamps('file-uuid', creation_time='2025-01-01T00:00:00Z')
    mock_clear.assert_called_once_with('file-uuid', 'file')
