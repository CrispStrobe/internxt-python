"""More API endpoint contract tests covering everything we missed in the
first pass: rename, delete, trash management, path-based lookup, network
upload chunk transports, restore_item, replace_file, WebDAV compat layer.
"""
from unittest.mock import MagicMock, patch

import pytest
import requests as requests_lib

from utils.api import api_client


@pytest.fixture
def capture():
    """Capture _make_request calls."""
    captured = []
    def fake(method, url, **kwargs):
        captured.append((method, url, kwargs))
        resp = MagicMock()
        resp.content = b'{}'
        resp.json.return_value = {}
        return resp
    with patch.object(api_client, '_make_request', side_effect=fake):
        yield captured


# ---------- rename_file / rename_folder ----------

def test_rename_file_with_type_includes_both_fields(capture):
    api_client.rename_file('uuid', 'newname', 'pdf')
    method, url, kwargs = capture[-1]
    assert method == 'PUT'
    assert url.endswith('/files/uuid/meta')
    assert kwargs['data']['plainName'] == 'newname'
    assert kwargs['data']['type'] == 'pdf'


def test_rename_file_without_type_omits_type(capture):
    api_client.rename_file('uuid', 'newname')
    _, _, kwargs = capture[-1]
    assert kwargs['data']['plainName'] == 'newname'
    assert 'type' not in kwargs['data']


def test_rename_folder_no_type_field(capture):
    api_client.rename_folder('uuid', 'NewFolder')
    method, url, kwargs = capture[-1]
    assert method == 'PUT'
    assert url.endswith('/folders/uuid/meta')
    assert kwargs['data'] == {'plainName': 'NewFolder'}


# ---------- delete_file / delete_folder (single-item DELETE) ----------

def test_delete_file_uses_files_endpoint(capture):
    api_client.delete_file('file-uuid')
    method, url, _ = capture[-1]
    assert method == 'DELETE'
    assert url.endswith('/files/file-uuid')


def test_delete_folder_uses_folders_endpoint(capture):
    api_client.delete_folder('folder-uuid')
    method, url, _ = capture[-1]
    assert method == 'DELETE'
    assert url.endswith('/folders/folder-uuid')


# ---------- trash management ----------

def test_get_trash_content_paginated(capture):
    api_client.get_trash_content(offset=20, limit=10, item_type='files')
    method, url, kwargs = capture[-1]
    assert method == 'GET'
    assert url.endswith('/storage/trash/paginated')
    assert kwargs['params'] == {'offset': 20, 'limit': 10, 'type': 'files'}


def test_get_trash_content_defaults(capture):
    api_client.get_trash_content()
    _, _, kwargs = capture[-1]
    assert kwargs['params']['offset'] == 0
    assert kwargs['params']['limit'] == 50
    assert kwargs['params']['type'] == 'both'


def test_clear_trash_uses_delete_all(capture):
    api_client.clear_trash()
    method, url, _ = capture[-1]
    assert method == 'DELETE'
    assert url.endswith('/storage/trash/all')


def test_restore_item_payload(capture):
    api_client.restore_item('item-uuid', 'file', 'dest-folder-uuid')
    method, url, kwargs = capture[-1]
    assert method == 'POST'
    assert url.endswith('/trash/restore')
    assert kwargs['data'] == {
        'uuid': 'item-uuid',
        'type': 'file',
        'destinationFolderUuid': 'dest-folder-uuid',
    }


def test_restore_item_to_root_when_no_destination(capture):
    api_client.restore_item('item-uuid', 'folder')
    _, _, kwargs = capture[-1]
    assert kwargs['data']['destinationFolderUuid'] is None


# ---------- path-based lookups ----------

def test_get_folder_by_path_endpoint(capture):
    api_client.get_folder_by_path('/Documents/Reports')
    method, url, kwargs = capture[-1]
    assert method == 'GET'
    assert url.endswith('/folders/meta')
    assert kwargs['params'] == {'path': '/Documents/Reports'}


def test_get_file_by_path_endpoint(capture):
    api_client.get_file_by_path('/Docs/x.txt')
    method, url, kwargs = capture[-1]
    assert method == 'GET'
    assert url.endswith('/files/meta')
    assert kwargs['params'] == {'path': '/Docs/x.txt'}


# ---------- create_file_entry / replace_file ----------

def test_create_file_entry_endpoint(capture):
    payload = {'folderUuid': 'p', 'plainName': 'doc', 'size': 100}
    api_client.create_file_entry(payload)
    method, url, kwargs = capture[-1]
    assert method == 'POST'
    assert url.endswith('/files')
    assert kwargs['data'] == payload


def test_replace_file_uses_put_with_uuid(capture):
    api_client.replace_file('file-uuid', {'fileId': 'new', 'size': 200})
    method, url, kwargs = capture[-1]
    assert method == 'PUT'
    assert url.endswith('/files/file-uuid')
    assert kwargs['data'] == {'fileId': 'new', 'size': 200}


# ---------- network upload/download chunk (real PUT/GET to pre-signed URLs) ----------

def test_upload_chunk_uses_raw_put(capture):
    """upload_chunk talks directly to the pre-signed URL via requests.put,
    NOT through the session wrapper (no auth header pollution)."""
    fake_resp = MagicMock()
    fake_resp.raise_for_status.return_value = None
    with patch('utils.api.requests.put', return_value=fake_resp) as mock_put:
        api_client.upload_chunk('https://upload-url.example/blob', b"raw bytes")
    args, kwargs = mock_put.call_args
    assert args[0] == 'https://upload-url.example/blob'
    assert kwargs['data'] == b"raw bytes"
    assert kwargs['headers']['Content-Type'] == 'application/octet-stream'
    assert kwargs['timeout'] == 300


def test_upload_chunk_propagates_http_errors():
    fake_resp = MagicMock()
    fake_resp.raise_for_status.side_effect = requests_lib.exceptions.HTTPError("502")
    with patch('utils.api.requests.put', return_value=fake_resp):
        with pytest.raises(requests_lib.exceptions.HTTPError):
            api_client.upload_chunk('https://u/', b"data")


def test_download_chunk_uses_raw_get_returns_bytes():
    fake_resp = MagicMock()
    fake_resp.raise_for_status.return_value = None
    fake_resp.content = b"the encrypted bytes"
    with patch('utils.api.requests.get', return_value=fake_resp) as mock_get:
        out = api_client.download_chunk('https://download-url/blob')
    args, kwargs = mock_get.call_args
    assert args[0] == 'https://download-url/blob'
    assert kwargs['timeout'] == 300
    assert out == b"the encrypted bytes"


def test_download_chunk_propagates_http_errors():
    fake_resp = MagicMock()
    fake_resp.raise_for_status.side_effect = requests_lib.exceptions.HTTPError("404")
    with patch('utils.api.requests.get', return_value=fake_resp):
        with pytest.raises(requests_lib.exceptions.HTTPError):
            api_client.download_chunk('https://d/')


# ---------- health_check ----------

def test_health_check_returns_user_info_on_success():
    fake_user = {'uuid': 'u', 'email': 'u@ex.com'}
    with patch.object(api_client, 'get_user_info', return_value=fake_user):
        out = api_client.health_check()
    assert out == fake_user


def test_health_check_returns_error_dict_on_failure():
    with patch.object(api_client, 'get_user_info',
                      side_effect=ConnectionError("net")):
        out = api_client.health_check()
    assert out['status'] == 'error'
    assert 'failed' in out['message'].lower()


# ---------- WebDAV compat: move_item / rename_item / trash_item / update_file ----------

def test_compat_move_item_tries_file_first(capture):
    """Top-level move_item tries move_file, then falls back to move_folder."""
    with patch.object(api_client, 'move_file',
                      return_value={'success': True}) as mock_mv:
        api_client.move_item('uuid', 'dest')
    mock_mv.assert_called_once_with('uuid', 'dest')


def test_compat_move_item_falls_back_to_folder():
    with patch.object(api_client, 'move_file',
                      side_effect=ValueError("not file")), \
         patch.object(api_client, 'move_folder',
                      return_value={'success': True}) as mock_mvf:
        api_client.move_item('uuid', 'dest')
    mock_mvf.assert_called_once_with('uuid', 'dest')


def test_compat_rename_item_with_extension_calls_rename_file_with_type():
    with patch.object(api_client, 'rename_file',
                      return_value={'success': True}) as mock_rn:
        api_client.rename_item('uuid', 'foo.pdf')
    args, _ = mock_rn.call_args
    # rename_file('uuid', 'foo', 'pdf')
    assert args == ('uuid', 'foo', 'pdf')


def test_compat_rename_item_without_extension_calls_rename_file_no_type():
    with patch.object(api_client, 'rename_file',
                      return_value={'success': True}) as mock_rn:
        api_client.rename_item('uuid', 'README')
    args, _ = mock_rn.call_args
    # rename_file('uuid', 'README') — no type arg
    assert args == ('uuid', 'README')


def test_compat_rename_item_falls_back_to_folder():
    with patch.object(api_client, 'rename_file',
                      side_effect=ValueError("not file")), \
         patch.object(api_client, 'rename_folder',
                      return_value={'success': True}) as mock_rnf:
        api_client.rename_item('uuid', 'NewName')
    mock_rnf.assert_called_once()


def test_compat_trash_item_falls_back_to_folder():
    with patch.object(api_client, 'trash_file',
                      side_effect=ValueError("not file")), \
         patch.object(api_client, 'trash_folder',
                      return_value={'success': True}) as mock_tf:
        api_client.trash_item('uuid')
    mock_tf.assert_called_once()


def test_compat_update_file_payload(capture):
    """update_file (compat layer) wraps replace_file with fileId+size payload."""
    with patch.object(api_client, 'replace_file',
                      return_value={'success': True}) as mock_replace:
        api_client.update_file('file-uuid', 'new-net-id', 5000)
    args, _ = mock_replace.call_args
    assert args[0] == 'file-uuid'
    assert args[1] == {'fileId': 'new-net-id', 'size': 5000}
