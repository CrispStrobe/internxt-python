"""Final round of drive_service tests covering remaining gaps:
download_file_by_path, upload_single_item edge cases (empty target path,
unreadable file, invalid conflict mode), download_file with creation_time
warning, search recursion edge cases.
"""
from unittest.mock import patch

import pytest

from services.drive import drive_service


class _FakeStream:
    """Minimal stand-in for a streaming requests.Response (download_stream)."""

    def __init__(self, data):
        self._data = data

    def iter_content(self, chunk_size=None):
        cs = chunk_size or (4 * 1024 * 1024)
        for i in range(0, len(self._data), cs):
            yield self._data[i:i + cs]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _reset():
    drive_service.folder_content_cache.clear()
    drive_service._mem_reserved = 0
    yield
    drive_service.folder_content_cache.clear()
    drive_service._mem_reserved = 0


# ---------- download_file_by_path ----------

def test_download_file_by_path_resolves_then_calls_download_file():
    resolved = {'type': 'file', 'uuid': 'fid', 'metadata': {},
                'path': '/Documents/x.pdf'}
    with patch.object(drive_service, 'resolve_path', return_value=resolved), \
         patch.object(drive_service, 'download_file',
                      return_value='/tmp/x.pdf') as mock_dl:
        out = drive_service.download_file_by_path('/Documents/x.pdf')
    mock_dl.assert_called_once()
    assert out == '/tmp/x.pdf'


def test_download_file_by_path_default_destination_uses_filename():
    """If no destination specified, use ./<filename>."""
    resolved = {'type': 'file', 'uuid': 'fid', 'metadata': {},
                'path': '/Documents/report.pdf'}
    captured = {}
    def fake_dl(uuid, dest, **kw):
        captured['dest'] = dest
        return dest
    with patch.object(drive_service, 'resolve_path', return_value=resolved), \
         patch.object(drive_service, 'download_file', side_effect=fake_dl):
        drive_service.download_file_by_path('/Documents/report.pdf')
    assert captured['dest'].endswith('report.pdf')


def test_download_file_by_path_rejects_folder():
    resolved = {'type': 'folder', 'uuid': 'fid', 'metadata': {}, 'path': '/D'}
    with patch.object(drive_service, 'resolve_path', return_value=resolved):
        with pytest.raises(ValueError, match="folder, not a file"):
            drive_service.download_file_by_path('/D')


# ---------- upload_single_item edge cases ----------

def test_upload_single_item_unreadable_file_returns_error(tmp_path):
    """If stat() raises (e.g., permission denied), return 'error' not crash."""
    f = tmp_path / "broken.txt"
    f.write_bytes(b"x")

    with patch('pathlib.Path.is_file', return_value=True), \
         patch('pathlib.Path.stat', side_effect=PermissionError("denied")):
        result = drive_service.upload_single_item_with_conflict_handling(
            f, '/Docs', 'parent-uuid', on_conflict='skip',
        )
    assert result == "error"


def test_upload_single_item_target_path_normalization(tmp_path):
    """The full target remote path must always start with exactly one '/'."""
    f = tmp_path / "doc.txt"
    f.write_bytes(b"x")

    captured_paths = []
    def fake_resolve(path):
        captured_paths.append(path)
        raise FileNotFoundError(path)

    with patch.object(drive_service, 'resolve_path', side_effect=fake_resolve), \
         patch.object(drive_service, 'upload_file_to_folder',
                      return_value={'uuid': 'x'}):
        # Pass a parent path with NO leading slash → should normalize
        drive_service.upload_single_item_with_conflict_handling(
            f, 'Docs/Sub', 'parent-uuid', on_conflict='skip',
        )

    # The looked-up path must always have exactly one leading slash
    assert all(p.startswith('/') for p in captured_paths)
    assert all(not p.startswith('//') for p in captured_paths)


def test_upload_single_item_resolve_unexpected_error_continues(tmp_path):
    """If resolve_path raises something other than FileNotFoundError,
    log warning and proceed with upload (not skip)."""
    f = tmp_path / "doc.txt"
    f.write_bytes(b"x")

    with patch.object(drive_service, 'resolve_path',
                      side_effect=ConnectionError("flaky API")), \
         patch.object(drive_service, 'upload_file_to_folder',
                      return_value={'uuid': 'x'}) as mock_up:
        result = drive_service.upload_single_item_with_conflict_handling(
            f, '/Docs', 'parent-uuid', on_conflict='skip',
        )
    # Despite the resolve error, upload still proceeded
    assert result == "uploaded"
    mock_up.assert_called_once()


# ---------- download_file metadata-only branches ----------

def test_download_file_creation_time_warning_branch(tmp_path):
    """If preserve_timestamps=True and only creation_time is present (no
    modification_time), the function still completes — it just warns."""
    fake_creds = {
        'user': {
            'bucket': '00' * 12,
            'mnemonic': ('abandon abandon abandon abandon abandon abandon '
                         'abandon abandon abandon abandon abandon about'),
            'bridgeUser': 'u@example.com',
            'userId': 'u',
        },
    }
    payload = b"content for ctime test"
    enc, idx_hex = drive_service.crypto.encrypt_stream_internxt_protocol(
        payload, fake_creds['user']['mnemonic'],
        fake_creds['user']['bucket'])

    metadata = {
        'uuid': 'fid', 'bucket': fake_creds['user']['bucket'],
        'fileId': 'nid', 'size': len(payload),
        'plainName': 'doc', 'type': 'txt',
        'creationTime': '2024-01-15T12:00:00Z',
        # No modificationTime
    }

    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with patch.object(drive_service.auth, 'get_auth_details',
                      return_value=fake_creds), \
         patch.object(drive_service.api, 'get_file_metadata',
                      return_value=metadata), \
         patch.object(drive_service.api, 'get_download_links',
                      return_value={'shards': [{'url': 'u'}], 'index': idx_hex}), \
         patch.object(drive_service.api, 'download_stream',
                      side_effect=lambda url, timeout=300: _FakeStream(enc)):
        out_path = drive_service.download_file('fid', str(out_dir),
                                               preserve_timestamps=True)
    # File downloaded successfully despite no modification time
    from pathlib import Path
    assert Path(out_path).exists()
    assert Path(out_path).read_bytes() == payload


# ---------- find_files at deeper depths ----------

def test_find_files_max_depth_2_goes_one_level_deeper():
    """max_depth=2 → search start folder and one level of subfolders."""
    tree = {
        'root-uuid': {
            'folders': [{'uuid': 'a', 'plainName': 'A'}],
            'files': [{'uuid': 'top', 'plainName': 'top', 'type': 'pdf', 'size': 1}],
        },
        'a': {
            'folders': [{'uuid': 'b', 'plainName': 'B'}],
            'files': [{'uuid': 'mid', 'plainName': 'mid', 'type': 'pdf', 'size': 1}],
        },
        'b': {
            'folders': [],
            'files': [{'uuid': 'deep', 'plainName': 'deep', 'type': 'pdf', 'size': 1}],
        },
    }
    drive_service.folder_content_cache.clear()
    fake_creds = {'user': {'rootFolderId': 'root-uuid'}}

    def fake_get_content(uuid):
        return tree.get(uuid, {'folders': [], 'files': []})

    with patch.object(drive_service.auth, 'get_auth_details',
                      return_value=fake_creds), \
         patch.object(drive_service, 'get_folder_content',
                      side_effect=fake_get_content):
        results = drive_service.find_files('*.pdf', '/', max_depth=2)

    # Top + mid found, deep filtered out
    names = sorted(r['display_name'] for r in results)
    assert names == ['mid.pdf', 'top.pdf']


def test_find_files_handles_listing_error_in_subfolder():
    """If one subfolder fails to list, others must still be searched."""
    fake_creds = {'user': {'rootFolderId': 'root-uuid'}}

    call_count = {'n': 0}
    def fake_list(path):
        call_count['n'] += 1
        if path == '/':
            return {
                'folders': [{'uuid': 'good', 'plainName': 'Good',
                             'path': '/Good', 'display_name': 'Good'},
                            {'uuid': 'bad', 'plainName': 'Bad',
                             'path': '/Bad', 'display_name': 'Bad'}],
                'files': [],
            }
        if path == '/Bad':
            raise ConnectionError("listing failed")
        # /Good
        return {
            'folders': [],
            'files': [{'uuid': 'g', 'plainName': 'g', 'type': 'pdf', 'size': 1,
                       'display_name': 'g.pdf'}],
        }

    with patch.object(drive_service.auth, 'get_auth_details',
                      return_value=fake_creds), \
         patch.object(drive_service, 'list_folder_with_paths',
                      side_effect=fake_list):
        results = drive_service.find_files('*.pdf', '/')

    # Found the file in /Good even though /Bad errored
    assert any(r['display_name'] == 'g.pdf' for r in results)


# ---------- copy_folder ----------

def test_copy_folder_creates_structure_and_copies_files():
    """copy_folder must create the folder, recurse into subfolders, and copy files."""
    fake_folder_meta = {'plainName': 'Src', 'uuid': 'src-uuid',
                        'creationTime': '2025-01-01T00:00:00Z'}
    fake_new_folder = {'uuid': 'new-uuid', 'plainName': 'Src'}
    fake_content = {
        'files': [{'uuid': 'file-1', 'plainName': 'a.txt', 'type': 'txt'}],
        'folders': [{'uuid': 'sub-1', 'plainName': 'Sub'}],
    }
    # Subfolder is empty
    fake_sub_meta = {'plainName': 'Sub', 'uuid': 'sub-1'}
    fake_sub_folder = {'uuid': 'newsub-uuid', 'plainName': 'Sub'}
    fake_sub_content = {'files': [], 'folders': []}

    call_count = {'get_folder_metadata': 0, 'create_folder': 0,
                  'get_folder_content': 0}

    def fake_get_meta(uuid):
        call_count['get_folder_metadata'] += 1
        return fake_folder_meta if uuid == 'src-uuid' else fake_sub_meta

    def fake_create(name, parent_folder_uuid=None, creation_time=None,
                    modification_time=None):
        call_count['create_folder'] += 1
        if name == 'Src':
            return fake_new_folder
        return fake_sub_folder

    def fake_get_content(uuid):
        call_count['get_folder_content'] += 1
        return fake_content if uuid == 'src-uuid' else fake_sub_content

    with patch.object(drive_service.api, 'get_folder_metadata', side_effect=fake_get_meta), \
         patch.object(drive_service, 'create_folder', side_effect=fake_create), \
         patch.object(drive_service, 'get_folder_content', side_effect=fake_get_content), \
         patch.object(drive_service, 'copy_item', return_value={'success': True}) as mock_copy:
        result = drive_service.copy_folder('src-uuid', 'dest-parent')

    assert result['success'] is True
    assert result['files_copied'] == 1
    assert result['folders_copied'] == 1
    assert result['uuid'] == 'new-uuid'
    # copy_item was called for the file
    mock_copy.assert_called_once_with('file-1', 'new-uuid')


def test_copy_folder_empty():
    """Copying an empty folder should succeed with 0 files/folders."""
    with patch.object(drive_service.api, 'get_folder_metadata',
                      return_value={'plainName': 'E', 'uuid': 'e'}), \
         patch.object(drive_service, 'create_folder',
                      return_value={'uuid': 'new-e', 'plainName': 'E'}), \
         patch.object(drive_service, 'get_folder_content',
                      return_value={'files': [], 'folders': []}):
        result = drive_service.copy_folder('e', 'parent')

    assert result['success'] is True
    assert result['files_copied'] == 0
    assert result['folders_copied'] == 0
