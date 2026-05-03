"""Tests for drive_service.upload_single_item_with_conflict_handling.

This is the per-file decision logic: should we skip / overwrite / fail?
We patch out the actual upload (the network-touching part) and verify the
function returns the right outcome string for each combination of remote
state + on_conflict policy.
"""
from pathlib import Path
from unittest.mock import patch

import pytest

from services.drive import drive_service


@pytest.fixture
def small_file(tmp_path):
    p = tmp_path / "doc.txt"
    p.write_bytes(b"hello")
    return p


@pytest.fixture
def empty_file(tmp_path):
    p = tmp_path / "empty.txt"
    p.write_bytes(b"")
    return p


def _patch_resolve(side_effect=None, return_value=None):
    """Helper: stub resolve_path so we control what 'exists remotely'."""
    if side_effect is not None:
        return patch.object(drive_service, 'resolve_path', side_effect=side_effect)
    return patch.object(drive_service, 'resolve_path', return_value=return_value)


# ---------- happy path ----------

def test_upload_returns_uploaded_when_target_missing(small_file):
    with _patch_resolve(side_effect=FileNotFoundError("no such")), \
         patch.object(drive_service, 'upload_file_to_folder',
                      return_value={'uuid': 'new-uuid'}) as mock_upload:
        result = drive_service.upload_single_item_with_conflict_handling(
            small_file, '/Docs', 'parent-uuid', on_conflict='skip',
        )
    assert result == "uploaded"
    mock_upload.assert_called_once()


def test_upload_forwards_timestamps_to_upload_call(small_file):
    with _patch_resolve(side_effect=FileNotFoundError("no")), \
         patch.object(drive_service, 'upload_file_to_folder',
                      return_value={'uuid': 'x'}) as mock_upload:
        drive_service.upload_single_item_with_conflict_handling(
            small_file, '/Docs', 'parent-uuid', on_conflict='skip',
            creation_time='2026-01-01T00:00:00Z',
            modification_time='2026-01-02T00:00:00Z',
        )
    _, kwargs = mock_upload.call_args
    assert kwargs.get('creation_time') == '2026-01-01T00:00:00Z'
    assert kwargs.get('modification_time') == '2026-01-02T00:00:00Z'


# ---------- pre-upload validation ----------

def test_directory_returns_skipped(tmp_path):
    """Passing a directory (not a file) is a no-op skip."""
    result = drive_service.upload_single_item_with_conflict_handling(
        tmp_path, '/Docs', 'parent-uuid', on_conflict='skip',
    )
    assert result == "skipped"


def test_nonexistent_path_returns_skipped(tmp_path):
    result = drive_service.upload_single_item_with_conflict_handling(
        tmp_path / "ghost.txt", '/Docs', 'parent-uuid', on_conflict='skip',
    )
    assert result == "skipped"


def test_empty_file_returns_skipped(empty_file):
    result = drive_service.upload_single_item_with_conflict_handling(
        empty_file, '/Docs', 'parent-uuid', on_conflict='skip',
    )
    assert result == "skipped"


def test_oversized_file_returns_error(small_file):
    """Anything > TWENTY_GIGABYTES short-circuits to error before upload."""
    with patch('pathlib.Path.stat') as mock_stat:
        fake = type('s', (), {'st_size': drive_service.TWENTY_GIGABYTES + 1})()
        mock_stat.return_value = fake
        # is_file() also goes through stat — make it return True via patch:
        with patch.object(Path, 'is_file', return_value=True):
            result = drive_service.upload_single_item_with_conflict_handling(
                small_file, '/Docs', 'parent-uuid', on_conflict='overwrite',
            )
    assert result == "error"


# ---------- on_conflict='skip' ----------

def test_skip_when_file_exists_remotely(small_file):
    existing = {'type': 'file', 'uuid': 'remote-file-uuid', 'path': '/Docs/doc.txt'}
    with _patch_resolve(return_value=existing), \
         patch.object(drive_service, 'upload_file_to_folder') as mock_upload:
        result = drive_service.upload_single_item_with_conflict_handling(
            small_file, '/Docs', 'parent-uuid', on_conflict='skip',
        )
    assert result == "skipped"
    mock_upload.assert_not_called()


# ---------- on_conflict='overwrite' ----------

def test_overwrite_deletes_existing_then_uploads(small_file):
    existing = {'type': 'file', 'uuid': 'old-uuid', 'path': '/Docs/doc.txt'}
    with _patch_resolve(return_value=existing), \
         patch.object(drive_service, 'delete_permanently_by_path',
                      return_value={'success': True}) as mock_del, \
         patch.object(drive_service, 'upload_file_to_folder',
                      return_value={'uuid': 'new'}) as mock_upload:
        result = drive_service.upload_single_item_with_conflict_handling(
            small_file, '/Docs', 'parent-uuid', on_conflict='overwrite',
        )
    assert result == "uploaded"
    mock_del.assert_called_once()
    mock_upload.assert_called_once()


def test_overwrite_refuses_when_target_is_folder(small_file):
    """Cannot replace a folder with a file — must error, not silently delete."""
    existing = {'type': 'folder', 'uuid': 'fold-uuid', 'path': '/Docs/doc.txt'}
    with _patch_resolve(return_value=existing), \
         patch.object(drive_service, 'delete_permanently_by_path') as mock_del, \
         patch.object(drive_service, 'upload_file_to_folder') as mock_upload:
        result = drive_service.upload_single_item_with_conflict_handling(
            small_file, '/Docs', 'parent-uuid', on_conflict='overwrite',
        )
    assert result == "error"
    mock_del.assert_not_called()
    mock_upload.assert_not_called()


def test_overwrite_returns_error_if_delete_fails(small_file):
    existing = {'type': 'file', 'uuid': 'x', 'path': '/Docs/doc.txt'}
    with _patch_resolve(return_value=existing), \
         patch.object(drive_service, 'delete_permanently_by_path',
                      side_effect=RuntimeError("server error")), \
         patch.object(drive_service, 'upload_file_to_folder') as mock_upload:
        result = drive_service.upload_single_item_with_conflict_handling(
            small_file, '/Docs', 'parent-uuid', on_conflict='overwrite',
        )
    assert result == "error"
    mock_upload.assert_not_called()


# ---------- invalid policy ----------

def test_invalid_conflict_mode_returns_error_when_target_exists(small_file):
    existing = {'type': 'file', 'uuid': 'x', 'path': '/Docs/doc.txt'}
    with _patch_resolve(return_value=existing):
        result = drive_service.upload_single_item_with_conflict_handling(
            small_file, '/Docs', 'parent-uuid', on_conflict='garbage-policy',
        )
    assert result == "error"


# ---------- upload itself fails ----------

def test_upload_exception_returns_error(small_file):
    with _patch_resolve(side_effect=FileNotFoundError("no")), \
         patch.object(drive_service, 'upload_file_to_folder',
                      side_effect=ConnectionError("boom")):
        result = drive_service.upload_single_item_with_conflict_handling(
            small_file, '/Docs', 'parent-uuid', on_conflict='skip',
        )
    assert result == "error"


# ---------- remote_filename override ----------

def test_remote_filename_override_used_for_conflict_check(small_file):
    """When uploading 'foo.txt' as 'bar.txt' on the remote, we should look
    up '/Docs/bar.txt', not '/Docs/foo.txt'."""
    captured_paths = []

    def fake_resolve(path):
        captured_paths.append(path)
        raise FileNotFoundError(path)

    with patch.object(drive_service, 'resolve_path', side_effect=fake_resolve), \
         patch.object(drive_service, 'upload_file_to_folder',
                      return_value={'uuid': 'x'}):
        drive_service.upload_single_item_with_conflict_handling(
            small_file, '/Docs', 'parent-uuid',
            on_conflict='skip', remote_filename='renamed.txt',
        )
    assert any('renamed.txt' in p for p in captured_paths)
