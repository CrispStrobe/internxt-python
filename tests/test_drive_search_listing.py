"""Tests for drive_service search/listing/path-construction helpers.

Covers: list_folder_with_paths, find_files (recursive), search_drive,
get_full_path_for_item.
"""
from unittest.mock import patch

import pytest

from services.drive import drive_service


def _set_root(uuid='root-uuid'):
    return patch.object(drive_service.auth, 'get_auth_details',
                        return_value={'user': {'rootFolderId': uuid}})


def _stub_folder_content(tree):
    """tree: {folder_uuid: {'folders': [...], 'files': [...]}}."""
    drive_service.folder_content_cache.clear()

    def fake(uuid):
        return tree.get(uuid, {'folders': [], 'files': []})

    return patch.object(drive_service, 'get_folder_content', side_effect=fake)


# ---------- list_folder_with_paths ----------

def test_list_folder_with_paths_attaches_path_and_size_display():
    tree = {
        'root-uuid': {
            'folders': [{'uuid': 'd1', 'plainName': 'Documents'}],
            'files': [
                {'uuid': 'f1', 'plainName': 'note', 'type': 'txt', 'size': 1024},
                {'uuid': 'f2', 'plainName': 'pic', 'type': 'jpg', 'size': '2048'},  # str size!
            ],
        },
    }
    with _set_root(), _stub_folder_content(tree):
        out = drive_service.list_folder_with_paths('/')

    assert out['current_path'] == '/'
    # Folders enriched with path + display_name
    folders = out['folders']
    assert folders[0]['path'] == '/Documents'
    assert folders[0]['display_name'] == 'Documents'
    assert folders[0]['size_display'] == '<DIR>'

    # Files have human-readable size and full display_name
    files = out['files']
    paths = {f['display_name']: f['path'] for f in files}
    assert paths['note.txt'] == '/note.txt'
    assert paths['pic.jpg'] == '/pic.jpg'

    # String sizes from the API must be coerced — no crash
    sizes = {f['display_name']: f['size_display'] for f in files}
    assert 'KB' in sizes['note.txt']  # 1024 → "1.0 KB"
    assert 'KB' in sizes['pic.jpg']  # 2048 → "2.0 KB"


def test_list_folder_with_paths_handles_garbage_size():
    """A file with non-numeric size must not crash — size_display falls back."""
    tree = {
        'root-uuid': {
            'folders': [],
            'files': [{'uuid': 'f1', 'plainName': 'broken', 'type': '', 'size': 'huh'}],
        },
    }
    with _set_root(), _stub_folder_content(tree):
        out = drive_service.list_folder_with_paths('/')
    assert out['files'][0]['size_display'] == '0 B'


def test_list_folder_with_paths_rejects_file_path():
    """list_folder_with_paths('/foo.txt') where foo.txt is a file must raise."""
    tree = {
        'root-uuid': {'folders': [], 'files': [{'uuid': 'fid', 'plainName': 'foo', 'type': 'txt'}]},
    }
    with _set_root(), _stub_folder_content(tree):
        with pytest.raises(ValueError, match="not a folder"):
            drive_service.list_folder_with_paths('/foo.txt')


# ---------- find_files (recursive client-side search) ----------

def test_find_files_finds_match_at_root():
    tree = {
        'root-uuid': {
            'folders': [],
            'files': [
                {'uuid': 'a', 'plainName': 'report', 'type': 'pdf', 'size': 100},
                {'uuid': 'b', 'plainName': 'photo', 'type': 'jpg', 'size': 200},
            ],
        },
    }
    with _set_root(), _stub_folder_content(tree):
        results = drive_service.find_files('*.pdf', '/')
    names = [r['display_name'] for r in results]
    assert names == ['report.pdf']


def test_find_files_recurses_into_subfolders():
    tree = {
        'root-uuid': {
            'folders': [{'uuid': 'sub', 'plainName': 'Sub'}],
            'files': [],
        },
        'sub': {
            'folders': [],
            'files': [{'uuid': 'a', 'plainName': 'deep', 'type': 'pdf', 'size': 100}],
        },
    }
    with _set_root(), _stub_folder_content(tree):
        results = drive_service.find_files('*.pdf', '/')
    assert len(results) == 1
    assert results[0]['display_name'] == 'deep.pdf'


def test_find_files_max_depth_limits_recursion():
    """max_depth=1 → search only the start folder, no subfolder descent."""
    tree = {
        'root-uuid': {
            'folders': [{'uuid': 'sub', 'plainName': 'Sub'}],
            'files': [{'uuid': 'a', 'plainName': 'top', 'type': 'pdf', 'size': 100}],
        },
        'sub': {
            'folders': [],
            'files': [{'uuid': 'b', 'plainName': 'deep', 'type': 'pdf', 'size': 100}],
        },
    }
    with _set_root(), _stub_folder_content(tree):
        results = drive_service.find_files('*.pdf', '/', max_depth=1)
    names = [r['display_name'] for r in results]
    assert names == ['top.pdf']  # 'deep.pdf' filtered by depth


def test_find_files_case_sensitivity_matters():
    tree = {
        'root-uuid': {
            'folders': [],
            'files': [{'uuid': 'a', 'plainName': 'IMG', 'type': 'JPG', 'size': 1}],
        },
    }
    with _set_root(), _stub_folder_content(tree):
        cs = drive_service.find_files('*.jpg', '/', case_sensitive=True)
        ci = drive_service.find_files('*.jpg', '/', case_sensitive=False)
    assert len(cs) == 0  # IMG.JPG ≠ *.jpg case-sensitive
    assert len(ci) == 1


def test_find_files_no_matches_returns_empty():
    tree = {
        'root-uuid': {'folders': [], 'files': [{'uuid': 'a', 'plainName': 'x', 'type': 'txt', 'size': 1}]},
    }
    with _set_root(), _stub_folder_content(tree):
        assert drive_service.find_files('*.pdf', '/') == []


# ---------- search_drive (server-side fuzzy) ----------

def test_search_drive_unwraps_data_field():
    fake_response = {'data': [
        {'itemId': 'a', 'name': 'item1', 'itemType': 'file'},
        {'itemId': 'b', 'name': 'item2', 'itemType': 'folder'},
    ]}
    with patch.object(drive_service.api, 'search_files',
                      return_value=fake_response):
        results = drive_service.search_drive('item')
    assert len(results) == 2


def test_search_drive_handles_results_field_alias():
    """Some API responses use 'results' instead of 'data'."""
    fake_response = {'results': [{'itemId': 'a', 'name': 'x', 'itemType': 'file'}]}
    with patch.object(drive_service.api, 'search_files', return_value=fake_response):
        results = drive_service.search_drive('x')
    assert len(results) == 1


def test_search_drive_returns_empty_for_unexpected_format():
    """If the API returns garbage, return [] rather than crash."""
    with patch.object(drive_service.api, 'search_files',
                      return_value={'wrong': 'shape'}):
        results = drive_service.search_drive('x')
    assert results == []


def test_search_drive_returns_empty_on_api_error():
    with patch.object(drive_service.api, 'search_files',
                      side_effect=ConnectionError("net")):
        assert drive_service.search_drive('x') == []


# ---------- get_full_path_for_item ----------

def test_full_path_for_root_item():
    """Item with no parent → '/<name>'."""
    item = {'plainName': 'top.txt', 'itemType': 'file'}
    out = drive_service.get_full_path_for_item(item)
    assert out == '/top.txt'


def test_full_path_for_file_appends_extension():
    item = {'plainName': 'doc', 'type': 'pdf', 'itemType': 'file'}
    out = drive_service.get_full_path_for_item(item)
    assert out == '/doc.pdf'


def test_full_path_for_nested_file_uses_ancestors():
    item = {
        'plainName': 'leaf', 'type': 'txt', 'itemType': 'file',
        'folderUuid': 'parent-uuid',
    }
    ancestors = [
        {'plainName': 'root', 'uuid': 'r'},
        {'plainName': 'A', 'uuid': 'a'},
        {'plainName': 'B', 'uuid': 'b'},
    ]
    with patch.object(drive_service.api, 'get_folder_ancestors',
                      return_value=ancestors):
        out = drive_service.get_full_path_for_item(item)
    # 'root' is filtered, then 'A' and 'B' joined
    assert out == '/A/B/leaf.txt'


def test_full_path_for_folder_uses_parentUuid():
    item = {'plainName': 'C', 'itemType': 'folder', 'parentUuid': 'p-uuid'}
    ancestors = [{'plainName': 'B', 'uuid': 'b'}]
    with patch.object(drive_service.api, 'get_folder_ancestors',
                      return_value=ancestors):
        out = drive_service.get_full_path_for_item(item)
    assert out == '/B/C'


def test_full_path_falls_back_to_question_mark_on_api_error():
    item = {'plainName': 'mystery.txt', 'folderUuid': 'p-uuid'}
    with patch.object(drive_service.api, 'get_folder_ancestors',
                      side_effect=ConnectionError("net")):
        out = drive_service.get_full_path_for_item(item)
    assert '?' in out
    assert 'mystery.txt' in out
