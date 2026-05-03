"""Tests for InternxtDAVResource mutating ops: delete, move_recursive,
copy_move; and InternxtDAVCollection.move_recursive (folder).

Also covers folder get_creation_date / get_last_modified accessors.
"""
from unittest.mock import patch

import pytest

from services.webdav_provider import (
    InternxtDAVResource,
    InternxtDAVCollection,
    webdav_api,
)


def _resource(path='/x.txt', metadata=None):
    r = InternxtDAVResource.__new__(InternxtDAVResource)
    r.path = path
    r.environ = {}
    r.file_metadata = metadata or {}
    r.provider = None
    r._upload_buffer = None
    return r


def _collection(path='/Folder', metadata=None):
    c = InternxtDAVCollection.__new__(InternxtDAVCollection)
    c.path = path
    c.environ = {}
    c.folder_metadata = metadata or {'uuid': 'fold-uuid'}
    c.provider = None
    c._content_cache = None
    c._content_cached_time = 0.0
    c.CACHE_TIMEOUT = 300
    return c


# ---------- InternxtDAVResource.delete ----------

def test_resource_delete_calls_trash_file():
    r = _resource(metadata={'uuid': 'real-uuid'})
    with patch('services.drive.drive_service.trash_file',
               return_value={'success': True}) as mock_trash, \
         patch.object(webdav_api, 'mark_deleted') as mock_mark:
        r.delete()
    mock_trash.assert_called_once_with('real-uuid')
    mock_mark.assert_called_once_with(r.path)


def test_resource_delete_pending_uuid_raises_dav_error():
    """A 'pending-' uuid means this resource was created but never uploaded
    — there's nothing on the server to delete."""
    from wsgidav.dav_error import DAVError
    r = _resource(metadata={'uuid': 'pending-foo.txt'})
    with pytest.raises(DAVError):
        r.delete()


def test_resource_delete_no_uuid_raises_dav_error():
    from wsgidav.dav_error import DAVError
    r = _resource(metadata={})
    with pytest.raises(DAVError):
        r.delete()


def test_resource_delete_wraps_drive_errors():
    from wsgidav.dav_error import DAVError
    r = _resource(metadata={'uuid': 'real-uuid'})
    with patch('services.drive.drive_service.trash_file',
               side_effect=ConnectionError("net")):
        with pytest.raises(DAVError):
            r.delete()


# ---------- InternxtDAVResource.move_recursive ----------

def test_resource_move_to_different_folder_calls_move_file():
    r = _resource(path='/Docs/x.txt',
                  metadata={'uuid': 'fid', 'plainName': 'x', 'type': 'txt'})
    dest_parent = {'type': 'folder', 'uuid': 'arch-uuid', 'metadata': {}}
    with patch('services.drive.drive_service.resolve_path',
               return_value=dest_parent), \
         patch('services.drive.drive_service.move_file',
               return_value={'success': True}) as mock_move, \
         patch('services.drive.drive_service.rename_file') as mock_rename, \
         patch.object(webdav_api, 'mark_deleted') as mock_mark:
        r.move_recursive('/Archive/x.txt')
    mock_move.assert_called_once_with('fid', 'arch-uuid')
    mock_rename.assert_not_called()
    mock_mark.assert_called_once_with('/Docs/x.txt')


def test_resource_rename_in_same_folder_calls_rename_file():
    r = _resource(path='/Docs/x.txt',
                  metadata={'uuid': 'fid', 'plainName': 'x', 'type': 'txt'})
    with patch('services.drive.drive_service.move_file') as mock_move, \
         patch('services.drive.drive_service.rename_file',
               return_value={'success': True}) as mock_rename, \
         patch.object(webdav_api, 'mark_deleted'):
        r.move_recursive('/Docs/y.txt')
    mock_move.assert_not_called()
    mock_rename.assert_called_once_with('fid', 'y.txt')


def test_resource_move_and_rename_simultaneously():
    """Different folder AND different name → both move_file and rename_file."""
    r = _resource(path='/Docs/x.txt',
                  metadata={'uuid': 'fid', 'plainName': 'x', 'type': 'txt'})
    dest_parent = {'type': 'folder', 'uuid': 'arch-uuid', 'metadata': {}}
    with patch('services.drive.drive_service.resolve_path',
               return_value=dest_parent), \
         patch('services.drive.drive_service.move_file',
               return_value={'success': True}) as mock_move, \
         patch('services.drive.drive_service.rename_file',
               return_value={'success': True}) as mock_rename, \
         patch.object(webdav_api, 'mark_deleted'):
        r.move_recursive('/Archive/y.txt')
    mock_move.assert_called_once()
    mock_rename.assert_called_once_with('fid', 'y.txt')


def test_resource_move_pending_uuid_raises_dav_error():
    from wsgidav.dav_error import DAVError
    r = _resource(metadata={'uuid': 'pending-x.txt'})
    with pytest.raises(DAVError):
        r.move_recursive('/Archive/x.txt')


def test_resource_move_wraps_drive_errors_as_dav_error():
    from wsgidav.dav_error import DAVError
    r = _resource(path='/Docs/x.txt',
                  metadata={'uuid': 'fid', 'plainName': 'x', 'type': 'txt'})
    with patch('services.drive.drive_service.resolve_path',
               side_effect=ConnectionError("net")):
        with pytest.raises(DAVError):
            r.move_recursive('/Archive/x.txt')


# ---------- InternxtDAVResource.copy_move ----------

def test_resource_copy_to_root_uses_root_folder_id():
    r = _resource(path='/x.txt', metadata={'uuid': 'fid'})
    creds = {'user': {'rootFolderId': 'root-uuid'}}
    with patch('services.auth.auth_service.get_auth_details',
               return_value=creds), \
         patch('services.drive.drive_service.copy_item',
               return_value={'success': True}) as mock_copy:
        r.copy_move('/x-copy.txt')
    mock_copy.assert_called_once_with('fid', 'root-uuid')


def test_resource_copy_to_subfolder_resolves_parent():
    r = _resource(path='/x.txt', metadata={'uuid': 'fid'})
    parent = {'type': 'folder', 'uuid': 'docs-uuid', 'metadata': {}}
    with patch('services.drive.drive_service.resolve_path',
               return_value=parent), \
         patch('services.drive.drive_service.copy_item',
               return_value={'success': True}) as mock_copy:
        r.copy_move('/Docs/x-copy.txt')
    mock_copy.assert_called_once_with('fid', 'docs-uuid')


def test_resource_copy_wraps_errors_as_dav_error():
    from wsgidav.dav_error import DAVError
    r = _resource(path='/x.txt', metadata={'uuid': 'fid'})
    creds = {'user': {'rootFolderId': 'root-uuid'}}
    with patch('services.auth.auth_service.get_auth_details', return_value=creds), \
         patch('services.drive.drive_service.copy_item',
               side_effect=ConnectionError("net")):
        with pytest.raises(DAVError):
            r.copy_move('/x-copy.txt')


# ---------- InternxtDAVCollection.move_recursive ----------

def test_folder_move_to_different_parent_calls_move_folder():
    c = _collection(path='/Sub', metadata={'uuid': 'sub-uuid', 'plainName': 'Sub'})
    dest_parent = {'type': 'folder', 'uuid': 'arch-uuid', 'metadata': {}}
    with patch('services.drive.drive_service.resolve_path',
               return_value=dest_parent), \
         patch('services.drive.drive_service.move_folder',
               return_value={'success': True}) as mock_move, \
         patch('services.drive.drive_service.rename_folder') as mock_rename, \
         patch.object(webdav_api, 'mark_deleted') as mock_mark:
        c.move_recursive('/Archive/Sub')
    mock_move.assert_called_once_with('sub-uuid', 'arch-uuid')
    mock_rename.assert_not_called()
    mock_mark.assert_called_once_with('/Sub')


def test_folder_rename_in_same_parent_calls_rename_folder():
    c = _collection(path='/Sub', metadata={'uuid': 'sub-uuid', 'plainName': 'Sub'})
    with patch('services.drive.drive_service.move_folder') as mock_move, \
         patch('services.drive.drive_service.rename_folder',
               return_value={'success': True}) as mock_rename, \
         patch.object(webdav_api, 'mark_deleted'):
        c.move_recursive('/Renamed')
    mock_move.assert_not_called()
    mock_rename.assert_called_once_with('sub-uuid', 'Renamed')


def test_folder_move_resolves_path_when_metadata_has_no_uuid():
    """Stale folder resource → resolve_path to discover uuid."""
    c = _collection(path='/Sub', metadata={'plainName': 'Sub'})  # no uuid

    def fake_resolve(path):
        if path == '/Sub':
            return {'type': 'folder', 'uuid': 'resolved-uuid', 'metadata': {}}
        return {'type': 'folder', 'uuid': 'arch-uuid', 'metadata': {}}

    with patch('services.drive.drive_service.resolve_path',
               side_effect=fake_resolve), \
         patch('services.drive.drive_service.move_folder',
               return_value={'success': True}) as mock_move, \
         patch.object(webdav_api, 'mark_deleted'):
        c.move_recursive('/Archive/Sub')
    mock_move.assert_called_once()
    args, _ = mock_move.call_args
    assert args[0] == 'resolved-uuid'


def test_folder_move_wraps_drive_errors_as_dav_error():
    from wsgidav.dav_error import DAVError
    c = _collection(path='/Sub', metadata={'uuid': 'sub-uuid', 'plainName': 'Sub'})
    with patch('services.drive.drive_service.resolve_path',
               side_effect=ConnectionError("net")):
        with pytest.raises(DAVError):
            c.move_recursive('/Archive/Sub')


# ---------- InternxtDAVCollection.copy_recursive (not implemented yet) ----------

def test_folder_copy_recursive_raises_dav_error_not_implemented():
    """Folder copy is intentionally unimplemented; must surface as 403."""
    from wsgidav.dav_error import DAVError
    c = _collection()
    with pytest.raises(DAVError):
        c.copy_recursive('/Sub-copy')


# ---------- InternxtDAVCollection date accessors ----------

def test_folder_get_creation_date_parses_iso_creationTime():
    c = _collection(metadata={'creationTime': '2024-01-15T12:00:00Z'})
    # Folder uses file_metadata for creation_date helper (shared method)
    c.file_metadata = c.folder_metadata
    ts = c.get_creation_date()
    assert abs(ts - 1705320000) < 2  # 2024-01-15T12:00:00Z


def test_folder_get_creation_date_falls_back_to_createdAt():
    c = _collection(metadata={'createdAt': '2024-06-01T00:00:00Z'})
    c.file_metadata = c.folder_metadata
    ts = c.get_creation_date()
    assert abs(ts - 1717200000) < 2  # 2024-06-01T00:00:00Z


def test_folder_get_last_modified_parses_updatedAt():
    c = _collection(metadata={'uuid': 'u', 'updatedAt': '2025-01-01T00:00:00Z'})
    ts = c.get_last_modified()
    # 2025-01-01T00:00:00Z = 1735689600
    assert abs(ts - 1735689600) < 2
