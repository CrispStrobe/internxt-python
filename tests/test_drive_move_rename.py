"""Tests for drive_service move/rename/check helpers + safety-pattern upload."""
from unittest.mock import patch

import pytest

from services.drive import drive_service


# ---------- move_file / move_folder ----------

def test_move_file_clears_caches_and_calls_api():
    drive_service.folder_content_cache['dest-uuid'] = (0, {'folders': [], 'files': []})
    with patch.object(drive_service, '_clear_parent_cache_for_item') as mock_src_clear, \
         patch.object(drive_service.api, 'move_file',
                      return_value={'ok': True}) as mock_api:
        result = drive_service.move_file('file-uuid', 'dest-uuid')

    mock_src_clear.assert_called_once_with('file-uuid', 'file')
    mock_api.assert_called_once_with('file-uuid', 'dest-uuid')
    assert result['success'] is True
    assert 'dest-uuid' not in drive_service.folder_content_cache
    drive_service.folder_content_cache.clear()


def test_move_folder_clears_caches_and_calls_api():
    drive_service.folder_content_cache['dest-uuid'] = (0, {'folders': [], 'files': []})
    with patch.object(drive_service, '_clear_parent_cache_for_item'), \
         patch.object(drive_service.api, 'move_folder',
                      return_value={'ok': True}) as mock_api:
        result = drive_service.move_folder('src-uuid', 'dest-uuid')
    mock_api.assert_called_once_with('src-uuid', 'dest-uuid')
    assert result['success'] is True
    assert 'dest-uuid' not in drive_service.folder_content_cache
    drive_service.folder_content_cache.clear()


def test_move_file_wraps_api_errors():
    with patch.object(drive_service, '_clear_parent_cache_for_item'), \
         patch.object(drive_service.api, 'move_file', side_effect=ConnectionError("net")):
        with pytest.raises(Exception, match="Failed to move file"):
            drive_service.move_file('uuid', 'dest')


# ---------- rename_file / rename_folder ----------

def test_rename_file_with_extension_splits_name():
    """rename_file('uuid', 'foo.pdf') → api.rename_file('uuid', 'foo', 'pdf')."""
    with patch.object(drive_service.api, 'rename_file',
                      return_value={'ok': True}) as mock_api, \
         patch.object(drive_service, '_clear_parent_cache_for_item'):
        drive_service.rename_file('uuid', 'foo.pdf')
    mock_api.assert_called_once_with('uuid', 'foo', 'pdf')


def test_rename_file_without_extension_passes_none():
    with patch.object(drive_service.api, 'rename_file',
                      return_value={'ok': True}) as mock_api, \
         patch.object(drive_service, '_clear_parent_cache_for_item'):
        drive_service.rename_file('uuid', 'README')
    mock_api.assert_called_once_with('uuid', 'README', None)


def test_rename_folder_calls_api_directly():
    with patch.object(drive_service.api, 'rename_folder',
                      return_value={'ok': True}) as mock_api, \
         patch.object(drive_service, '_clear_parent_cache_for_item'):
        drive_service.rename_folder('uuid', 'NewName')
    mock_api.assert_called_once_with('uuid', 'NewName')


def test_rename_file_wraps_api_errors():
    with patch.object(drive_service.api, 'rename_file',
                      side_effect=ConnectionError("net")):
        with pytest.raises(Exception, match="Failed to rename file"):
            drive_service.rename_file('uuid', 'foo.pdf')


# ---------- move_item / rename_item / trash_item (try-file-then-folder) ----------

def test_move_item_falls_back_to_folder_on_file_error():
    with patch.object(drive_service, 'move_file',
                      side_effect=Exception("not a file")), \
         patch.object(drive_service, 'move_folder',
                      return_value={'success': True}) as mock_mvf:
        result = drive_service.move_item('uuid', 'dest-uuid')
    mock_mvf.assert_called_once()
    assert result['success'] is True


def test_rename_item_falls_back_to_folder_on_file_error():
    with patch.object(drive_service, 'rename_file',
                      side_effect=Exception("not a file")), \
         patch.object(drive_service, 'rename_folder',
                      return_value={'success': True}) as mock_rnf:
        drive_service.rename_item('uuid', 'NewName')
    mock_rnf.assert_called_once()


def test_trash_item_falls_back_to_folder_on_file_error():
    with patch.object(drive_service.api, 'trash_file',
                      side_effect=Exception("not a file")), \
         patch.object(drive_service.api, 'trash_folder',
                      return_value={'success': True}) as mock_tf:
        drive_service.trash_item('uuid')
    mock_tf.assert_called_once()


# ---------- check_file_exists / check_folder_exists ----------

def test_check_file_exists_returns_info_for_file():
    file_info = {'type': 'file', 'uuid': 'f', 'metadata': {}}
    with patch.object(drive_service, 'resolve_path', return_value=file_info):
        out = drive_service.check_file_exists('/x.txt')
    assert out == file_info


def test_check_file_exists_returns_none_for_folder():
    folder_info = {'type': 'folder', 'uuid': 'd', 'metadata': {}}
    with patch.object(drive_service, 'resolve_path', return_value=folder_info):
        assert drive_service.check_file_exists('/Documents') is None


def test_check_file_exists_returns_none_when_missing():
    with patch.object(drive_service, 'resolve_path',
                      side_effect=FileNotFoundError("no")):
        assert drive_service.check_file_exists('/missing') is None


def test_check_file_exists_swallows_unexpected_errors():
    with patch.object(drive_service, 'resolve_path',
                      side_effect=ConnectionError("net")):
        assert drive_service.check_file_exists('/x') is None


def test_check_folder_exists_returns_info_for_folder():
    folder_info = {'type': 'folder', 'uuid': 'd', 'metadata': {}}
    with patch.object(drive_service, 'resolve_path', return_value=folder_info):
        assert drive_service.check_folder_exists('/Documents') == folder_info


def test_check_folder_exists_returns_none_for_file():
    file_info = {'type': 'file', 'uuid': 'f', 'metadata': {}}
    with patch.object(drive_service, 'resolve_path', return_value=file_info):
        assert drive_service.check_folder_exists('/x.txt') is None


# ---------- move_by_path ----------

def test_move_by_path_dispatches_file_to_api_move_file():
    src = {'type': 'file', 'uuid': 'src', 'metadata': {}, 'path': '/x.txt'}
    target = {'type': 'folder', 'uuid': 'tgt', 'metadata': {}, 'path': '/Archive'}

    def fake_resolve(p):
        return src if 'x.txt' in p else target

    with patch.object(drive_service, 'resolve_path', side_effect=fake_resolve), \
         patch.object(drive_service.api, 'move_file',
                      return_value={'ok': True}) as mock_mv:
        drive_service.move_by_path('/x.txt', '/Archive')
    mock_mv.assert_called_once_with('src', 'tgt')


def test_move_by_path_dispatches_folder_to_api_move_folder():
    src = {'type': 'folder', 'uuid': 'src-d', 'metadata': {}, 'path': '/Sub'}
    target = {'type': 'folder', 'uuid': 'tgt-d', 'metadata': {}, 'path': '/Archive'}

    def fake_resolve(p):
        return src if p == '/Sub' else target

    with patch.object(drive_service, 'resolve_path', side_effect=fake_resolve), \
         patch.object(drive_service.api, 'move_folder',
                      return_value={'ok': True}) as mock_mvf:
        drive_service.move_by_path('/Sub', '/Archive')
    mock_mvf.assert_called_once_with('src-d', 'tgt-d')


def test_move_by_path_rejects_target_that_is_file():
    src = {'type': 'file', 'uuid': 's', 'metadata': {}, 'path': '/x'}
    target = {'type': 'file', 'uuid': 't', 'metadata': {}, 'path': '/y'}

    def fake_resolve(p):
        return src if p == '/x' else target

    with patch.object(drive_service, 'resolve_path', side_effect=fake_resolve):
        with pytest.raises(ValueError, match="is a file"):
            drive_service.move_by_path('/x', '/y')


# ---------- upload_with_safety_pattern ----------

def test_safety_pattern_uploads_directly_when_no_existing_file(tmp_path):
    """No existing file at remote → straight upload, no backup."""
    local = tmp_path / "doc.txt"
    local.write_bytes(b"x")

    with patch.object(drive_service, 'resolve_path',
                      side_effect=FileNotFoundError("no")), \
         patch.object(drive_service, 'upload_file_to_folder',
                      return_value={'uuid': 'new'}) as mock_upload, \
         patch.object(drive_service.api, 'rename_file') as mock_rename, \
         patch.object(drive_service.api, 'delete_permanently') as mock_del:
        drive_service.upload_with_safety_pattern(local, 'remote-uuid', 'doc.txt')
    mock_upload.assert_called_once()
    mock_rename.assert_not_called()
    mock_del.assert_not_called()


def test_safety_pattern_backs_up_then_purges_on_success(tmp_path):
    """Existing file → rename to .bak-XXXXXX, upload, then permanently delete backup."""
    local = tmp_path / "doc.txt"
    local.write_bytes(b"x")

    existing = {'type': 'file', 'uuid': 'old-uuid', 'metadata': {}}
    with patch.object(drive_service, 'resolve_path', return_value=existing), \
         patch.object(drive_service, 'upload_file_to_folder',
                      return_value={'uuid': 'new'}), \
         patch.object(drive_service.api, 'rename_file') as mock_rename, \
         patch.object(drive_service.api, 'delete_permanently') as mock_del:
        drive_service.upload_with_safety_pattern(local, 'remote-uuid', 'doc.txt')

    # Backup name pattern: foo.txt.bak-XXXXXX
    rename_args = mock_rename.call_args[0]
    assert rename_args[0] == 'old-uuid'
    assert rename_args[1].startswith('doc.txt.bak-')
    # After successful upload, backup is permanently deleted
    mock_del.assert_called_once_with('old-uuid', 'file')


def test_safety_pattern_rolls_back_backup_on_upload_failure(tmp_path):
    """Existing file → backup → upload fails → rename backup back to original."""
    local = tmp_path / "doc.txt"
    local.write_bytes(b"x")

    existing = {'type': 'file', 'uuid': 'old-uuid', 'metadata': {}}
    with patch.object(drive_service, 'resolve_path', return_value=existing), \
         patch.object(drive_service, 'upload_file_to_folder',
                      side_effect=ConnectionError("net")), \
         patch.object(drive_service.api, 'rename_file') as mock_rename, \
         patch.object(drive_service.api, 'delete_permanently') as mock_del:
        with pytest.raises(ConnectionError):
            drive_service.upload_with_safety_pattern(local, 'remote-uuid', 'doc.txt')

    # rename_file called twice: once to back up, once to roll back
    assert mock_rename.call_count == 2
    # Second call (rollback) restores original name
    rollback_args = mock_rename.call_args_list[1][0]
    assert rollback_args == ('old-uuid', 'doc.txt')
    # Backup must NOT be deleted on failure
    mock_del.assert_not_called()


# ---------- copy_item ----------

def test_copy_item_preserves_timestamps_when_present():
    """copy_item: download then upload, threading the original timestamps
    through upload_file_to_folder (NOT a server-side copy)."""
    src_metadata = {
        'uuid': 'src-uuid', 'plainName': 'doc', 'type': 'pdf',
        'creationTime': '2025-01-01T00:00:00Z',
        'modificationTime': '2025-06-01T00:00:00Z',
    }

    with patch.object(drive_service.api, 'get_file_metadata',
                      return_value=src_metadata), \
         patch.object(drive_service, 'download_file',
                      return_value='/tmp/fake') as mock_dl, \
         patch.object(drive_service, 'upload_file_to_folder',
                      return_value={'uuid': 'new-uuid'}) as mock_up:
        result = drive_service.copy_item('src-uuid', 'dest-folder-uuid')

    mock_dl.assert_called_once()
    _, kwargs = mock_up.call_args
    args, _ = mock_up.call_args
    # upload_file_to_folder(temp_path, dest_folder_uuid, plain_name, file_type, ...)
    assert args[1] == 'dest-folder-uuid'
    assert args[2] == 'doc'
    assert args[3] == 'pdf'
    assert kwargs['creation_time'] == '2025-01-01T00:00:00Z'
    assert kwargs['modification_time'] == '2025-06-01T00:00:00Z'
    assert result['success'] is True
    assert result['timestamps_preserved'] is True


def test_copy_item_falls_back_to_legacy_timestamp_keys():
    """If only createdAt/updatedAt are present (no creationTime/modificationTime),
    use those instead."""
    src_metadata = {
        'uuid': 'src', 'plainName': 'x', 'type': 'txt',
        'createdAt': '2024-01-01T00:00:00Z',
        'updatedAt': '2024-06-01T00:00:00Z',
    }
    with patch.object(drive_service.api, 'get_file_metadata',
                      return_value=src_metadata), \
         patch.object(drive_service, 'download_file', return_value='/tmp/x'), \
         patch.object(drive_service, 'upload_file_to_folder',
                      return_value={'uuid': 'new'}) as mock_up:
        drive_service.copy_item('src', 'dest')
    _, kwargs = mock_up.call_args
    assert kwargs['creation_time'] == '2024-01-01T00:00:00Z'
    assert kwargs['modification_time'] == '2024-06-01T00:00:00Z'


def test_copy_item_wraps_errors():
    with patch.object(drive_service.api, 'get_file_metadata',
                      side_effect=ConnectionError("net")):
        with pytest.raises(Exception, match="Copy failed"):
            drive_service.copy_item('uuid', 'dest')
