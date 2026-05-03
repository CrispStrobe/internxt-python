"""Tests for InternxtDAVCollection mutating operations and WebDAVAPIClient
isolated-session bookkeeping.
"""
from unittest.mock import MagicMock, patch

import pytest

from services.webdav_provider import (
    InternxtDAVCollection,
    WebDAVAPIClient,
    webdav_api,
)


class _FakeProvider:
    pass


_ENV = {'wsgidav.provider': _FakeProvider()}


def _collection(path='/', metadata=None):
    c = InternxtDAVCollection.__new__(InternxtDAVCollection)
    c.path = path
    c.environ = _ENV
    c.folder_metadata = metadata or {}
    c.provider = None
    c._content_cache = None
    c._content_cached_time = 0.0
    c.CACHE_TIMEOUT = 300
    return c


# ---------- create_collection (mkdir) ----------

def test_create_collection_at_root_uses_root_folder_id():
    c = _collection('/')
    fake_creds = {'user': {'rootFolderId': 'root-uuid'}}
    new_folder = {'uuid': 'new-uuid', 'plainName': 'NewDir'}
    with patch('services.auth.auth_service.get_auth_details', return_value=fake_creds), \
         patch('services.drive.drive_service.create_folder',
               return_value=new_folder) as mock_create:
        result = c.create_collection('NewDir')

    mock_create.assert_called_once_with('NewDir', 'root-uuid')
    assert isinstance(result, InternxtDAVCollection)
    assert result.path == '/NewDir'


def test_create_collection_in_subfolder_uses_resolved_uuid():
    c = _collection('/Documents')
    resolved = {'type': 'folder', 'uuid': 'docs-uuid', 'metadata': {}}
    new_folder = {'uuid': 'sub-uuid', 'plainName': 'Sub'}
    with patch('services.drive.drive_service.resolve_path',
               return_value=resolved), \
         patch('services.drive.drive_service.create_folder',
               return_value=new_folder) as mock_create:
        result = c.create_collection('Sub')

    mock_create.assert_called_once_with('Sub', 'docs-uuid')
    assert result.path == '/Documents/Sub'


def test_create_collection_invalidates_content_cache():
    c = _collection('/')
    c._content_cache = {'folders': [], 'files': [], 'names': []}
    fake_creds = {'user': {'rootFolderId': 'root'}}
    with patch('services.auth.auth_service.get_auth_details', return_value=fake_creds), \
         patch('services.drive.drive_service.create_folder',
               return_value={'uuid': 'x', 'plainName': 'y'}):
        c.create_collection('y')
    assert c._content_cache is None


def test_create_collection_wraps_drive_errors_as_dav_error():
    """Anything that goes wrong in drive_service must surface as a DAVError
    with HTTP_FORBIDDEN, not bubble up as a raw exception."""
    from wsgidav.dav_error import DAVError

    c = _collection('/')
    with patch('services.auth.auth_service.get_auth_details',
               return_value={'user': {'rootFolderId': 'r'}}), \
         patch('services.drive.drive_service.create_folder',
               side_effect=RuntimeError("api down")):
        with pytest.raises(DAVError):
            c.create_collection('NewDir')


# ---------- delete (folder trash) ----------

def test_delete_calls_trash_folder_with_resolved_uuid():
    c = _collection('/Trash-me', metadata={'uuid': 'fold-uuid'})
    with patch('services.drive.drive_service.trash_folder',
               return_value={'success': True}) as mock_trash:
        c.delete()
    mock_trash.assert_called_once_with('fold-uuid')


def test_delete_resolves_path_when_metadata_has_no_uuid():
    """If metadata lacks uuid (stale resource), resolve_path first."""
    c = _collection('/Trash-me', metadata={})
    resolved = {'type': 'folder', 'uuid': 'resolved-uuid', 'metadata': {}}
    with patch('services.drive.drive_service.resolve_path',
               return_value=resolved), \
         patch('services.drive.drive_service.trash_folder',
               return_value={'success': True}) as mock_trash:
        c.delete()
    mock_trash.assert_called_once_with('resolved-uuid')


def test_delete_marks_path_as_recently_deleted():
    c = _collection('/will-be-gone', metadata={'uuid': 'd-uuid'})
    with patch('services.drive.drive_service.trash_folder',
               return_value={'success': True}), \
         patch.object(webdav_api, 'mark_deleted') as mock_mark:
        c.delete()
    mock_mark.assert_called_once_with('/will-be-gone')


def test_delete_wraps_errors_as_dav_error():
    from wsgidav.dav_error import DAVError
    c = _collection('/x', metadata={'uuid': 'd-uuid'})
    with patch('services.drive.drive_service.trash_folder',
               side_effect=RuntimeError("api down")):
        with pytest.raises(DAVError):
            c.delete()


# ---------- WebDAVAPIClient.get_folder_content ----------

def test_get_folder_content_returns_normalized_dict():
    api = WebDAVAPIClient()
    fake_isolated = MagicMock()
    fake_isolated.get_folder_folders.return_value = {'result': [{'uuid': 'a'}]}
    fake_isolated.get_folder_files.return_value = {'result': [{'uuid': 'b'}]}

    with patch.object(api, '_get_isolated_session', return_value=fake_isolated):
        out = api.get_folder_content('parent-uuid')
    assert out == {'folders': [{'uuid': 'a'}], 'files': [{'uuid': 'b'}]}


def test_get_folder_content_supports_legacy_keys():
    api = WebDAVAPIClient()
    fake_isolated = MagicMock()
    fake_isolated.get_folder_folders.return_value = {'folders': [{'uuid': 'a'}]}
    fake_isolated.get_folder_files.return_value = {'files': [{'uuid': 'b'}]}

    with patch.object(api, '_get_isolated_session', return_value=fake_isolated):
        out = api.get_folder_content('parent-uuid')
    assert out['folders'] == [{'uuid': 'a'}]
    assert out['files'] == [{'uuid': 'b'}]


def test_get_folder_content_returns_empty_on_error():
    """Network failure → empty dict, not propagated exception."""
    api = WebDAVAPIClient()
    with patch.object(api, '_get_isolated_session',
                      side_effect=ConnectionError("net")):
        out = api.get_folder_content('parent-uuid')
    assert out == {'folders': [], 'files': []}


# ---------- WebDAVAPIClient.get_credentials (lazy init) ----------

def test_get_credentials_lazily_loads_then_caches():
    api = WebDAVAPIClient()
    fake_creds = {'user': {'email': 'u@ex.com'}}
    with patch('services.auth.auth_service.get_auth_details',
               return_value=fake_creds) as mock_auth:
        a = api.get_credentials()
        b = api.get_credentials()
    # Same dict, fetched only once
    assert a is b
    assert mock_auth.call_count == 1


# ---------- _get_isolated_session token-refresh path ----------

def test_isolated_session_creates_fresh_client_per_thread():
    api = WebDAVAPIClient()
    fake_creds = {'token': 't', 'newToken': 'nt', 'user': {'email': 'u'}}

    with patch('services.auth.auth_service.refresh_tokens'), \
         patch('services.auth.auth_service.get_auth_details',
               return_value=fake_creds):
        client_a = api._get_isolated_session()
        client_b = api._get_isolated_session()  # cached on thread-local

    assert client_a is client_b


def test_isolated_session_continues_on_refresh_failure():
    """If refresh_tokens fails (e.g. server briefly down), we still
    proceed with the (possibly stale) tokens."""
    api = WebDAVAPIClient()
    fake_creds = {'token': 'old', 'newToken': 'old-new', 'user': {'email': 'u'}}

    with patch('services.auth.auth_service.refresh_tokens',
               side_effect=ConnectionError("temporary")), \
         patch('services.auth.auth_service.get_auth_details',
               return_value=fake_creds):
        # Should not raise
        client = api._get_isolated_session()
    assert client is not None
