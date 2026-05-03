"""Tests for drive_service trash/delete dispatch + pagination helpers."""
from unittest.mock import patch

import pytest

from services.drive import drive_service


# ---------- trash_file / trash_folder ----------

def test_trash_file_clears_parent_cache_then_calls_api():
    with patch.object(drive_service, '_clear_parent_cache_for_item') as mock_clear, \
         patch.object(drive_service.api, 'trash_file', return_value={'ok': True}) as mock_api:
        result = drive_service.trash_file('file-uuid')
    mock_clear.assert_called_once_with('file-uuid', 'file')
    mock_api.assert_called_once_with('file-uuid')
    assert result['success'] is True
    assert 'moved to trash' in result['message']
    assert result['file']['uuid'] == 'file-uuid'


def test_trash_folder_clears_parent_cache_then_calls_api():
    with patch.object(drive_service, '_clear_parent_cache_for_item') as mock_clear, \
         patch.object(drive_service.api, 'trash_folder', return_value={}) as mock_api:
        result = drive_service.trash_folder('folder-uuid')
    mock_clear.assert_called_once_with('folder-uuid', 'folder')
    mock_api.assert_called_once_with('folder-uuid')
    assert result['success'] is True
    assert result['folder']['uuid'] == 'folder-uuid'


def test_trash_file_wraps_api_errors():
    with patch.object(drive_service, '_clear_parent_cache_for_item'), \
         patch.object(drive_service.api, 'trash_file', side_effect=ConnectionError("net")):
        with pytest.raises(Exception, match="Failed to trash file"):
            drive_service.trash_file('uuid')


# ---------- trash_by_path / delete_permanently_by_path dispatch ----------

def test_trash_by_path_dispatches_to_file_when_file():
    resolved = {'type': 'file', 'uuid': 'f-uuid', 'metadata': {}}
    with patch.object(drive_service, 'resolve_path', return_value=resolved), \
         patch.object(drive_service, 'trash_file', return_value={'success': True}) as mock_tf, \
         patch.object(drive_service, 'trash_folder') as mock_tdir:
        drive_service.trash_by_path('/x.txt')
    mock_tf.assert_called_once_with('f-uuid')
    mock_tdir.assert_not_called()


def test_trash_by_path_dispatches_to_folder_when_folder():
    resolved = {'type': 'folder', 'uuid': 'd-uuid', 'metadata': {}}
    with patch.object(drive_service, 'resolve_path', return_value=resolved), \
         patch.object(drive_service, 'trash_file') as mock_tf, \
         patch.object(drive_service, 'trash_folder', return_value={'success': True}) as mock_tdir:
        drive_service.trash_by_path('/SomeFolder')
    mock_tdir.assert_called_once_with('d-uuid')
    mock_tf.assert_not_called()


def test_delete_permanently_by_path_dispatches_to_file_when_file():
    resolved = {'type': 'file', 'uuid': 'f-uuid'}
    with patch.object(drive_service, 'resolve_path', return_value=resolved), \
         patch.object(drive_service, 'delete_permanently_file',
                      return_value={'success': True}) as mock_df:
        drive_service.delete_permanently_by_path('/x.txt')
    mock_df.assert_called_once_with('f-uuid')


def test_delete_permanently_by_path_dispatches_to_folder_when_folder():
    resolved = {'type': 'folder', 'uuid': 'd-uuid'}
    with patch.object(drive_service, 'resolve_path', return_value=resolved), \
         patch.object(drive_service, 'delete_permanently_folder',
                      return_value={'success': True}) as mock_dd:
        drive_service.delete_permanently_by_path('/folder')
    mock_dd.assert_called_once_with('d-uuid')


# ---------- _get_all_folders pagination ----------

def test_get_all_folders_single_page():
    """If the page is shorter than the limit, no recursion."""
    page = [{'uuid': f'u{i}', 'plainName': f'f{i}'} for i in range(10)]
    with patch.object(drive_service.api, 'get_folder_folders',
                      return_value={'result': page}) as mock_api:
        result = drive_service._get_all_folders('parent-uuid')
    assert len(result) == 10
    mock_api.assert_called_once()


def test_get_all_folders_multiple_pages():
    """Full page (50) → must recurse for the next page."""
    full_page = [{'uuid': f'u{i}', 'plainName': f'f{i}'} for i in range(50)]
    last_page = [{'uuid': 'u50', 'plainName': 'last'}]

    call_offsets = []
    def fake_api(folder_uuid, offset, limit):
        call_offsets.append(offset)
        if offset == 0:
            return {'result': full_page}
        return {'result': last_page}

    with patch.object(drive_service.api, 'get_folder_folders', side_effect=fake_api):
        result = drive_service._get_all_folders('parent-uuid')
    assert call_offsets == [0, 50]
    assert len(result) == 51


def test_get_all_folders_returns_empty_on_api_error():
    with patch.object(drive_service.api, 'get_folder_folders',
                      side_effect=ConnectionError("net")):
        result = drive_service._get_all_folders('parent-uuid')
    assert result == []


def test_get_all_folders_handles_legacy_folders_key():
    """Some API responses use 'folders' instead of 'result'."""
    with patch.object(drive_service.api, 'get_folder_folders',
                      return_value={'folders': [{'uuid': 'x', 'plainName': 'A'}]}):
        result = drive_service._get_all_folders('parent-uuid')
    assert len(result) == 1
    assert result[0]['uuid'] == 'x'


def test_get_all_files_single_page():
    page = [{'uuid': f'u{i}', 'plainName': f'f{i}'} for i in range(5)]
    with patch.object(drive_service.api, 'get_folder_files',
                      return_value={'result': page}):
        result = drive_service._get_all_files('parent-uuid')
    assert len(result) == 5


def test_get_all_files_multiple_pages():
    full_page = [{'uuid': f'u{i}', 'plainName': f'f{i}'} for i in range(50)]
    last_page = [{'uuid': 'u50'}]

    def fake_api(folder_uuid, offset, limit):
        return {'result': full_page if offset == 0 else last_page}

    with patch.object(drive_service.api, 'get_folder_files', side_effect=fake_api):
        result = drive_service._get_all_files('parent-uuid')
    assert len(result) == 51


# ---------- _clear_parent_cache_for_item ----------

def test_clear_parent_cache_for_file_removes_from_cache():
    drive_service.folder_content_cache['parent-uuid'] = (0, {'folders': [], 'files': []})
    with patch.object(drive_service.api, 'get_file_metadata',
                      return_value={'folderUuid': 'parent-uuid'}):
        drive_service._clear_parent_cache_for_item('file-uuid', 'file')
    assert 'parent-uuid' not in drive_service.folder_content_cache


def test_clear_parent_cache_for_folder_removes_from_cache():
    drive_service.folder_content_cache['parent-uuid'] = (0, {'folders': [], 'files': []})
    with patch.object(drive_service.api, 'get_folder_metadata',
                      return_value={'parentUuid': 'parent-uuid'}):
        drive_service._clear_parent_cache_for_item('child-uuid', 'folder')
    assert 'parent-uuid' not in drive_service.folder_content_cache


def test_clear_parent_cache_swallows_metadata_lookup_errors():
    """Cache clearing is best-effort; errors fetching metadata must NOT propagate."""
    with patch.object(drive_service.api, 'get_file_metadata',
                      side_effect=ConnectionError("net")):
        drive_service._clear_parent_cache_for_item('file-uuid', 'file')
    # No exception → pass


# ---------- list_folder dispatches to root when no folder_uuid ----------

def test_list_folder_uses_root_uuid_when_no_argument():
    with patch.object(drive_service.auth, 'get_auth_details',
                      return_value={'user': {'rootFolderId': 'root-xyz'}}), \
         patch.object(drive_service, 'get_folder_content',
                      return_value={'folders': [], 'files': []}) as mock_get:
        drive_service.list_folder()
    mock_get.assert_called_once_with('root-xyz')


def test_list_folder_uses_provided_uuid():
    with patch.object(drive_service.auth, 'get_auth_details',
                      return_value={'user': {'rootFolderId': 'root-xyz'}}), \
         patch.object(drive_service, 'get_folder_content',
                      return_value={'folders': [], 'files': []}) as mock_get:
        drive_service.list_folder('explicit-uuid')
    mock_get.assert_called_once_with('explicit-uuid')


def test_list_folder_raises_when_no_root_id():
    with patch.object(drive_service.auth, 'get_auth_details',
                      return_value={'user': {}}):
        with pytest.raises(ValueError, match="No root folder"):
            drive_service.list_folder()
