"""Path resolution + recursive folder creation tests for drive_service.

These exercise the routing logic by faking the underlying ApiClient and
auth_service, so no network is involved.
"""
from unittest.mock import patch

import pytest

from services.drive import drive_service


# ---------- helpers ----------

def _set_root(uuid='root-uuid'):
    """Patch auth so resolve_path / create_folder_recursive find a root id."""
    return patch.object(
        drive_service.auth, 'get_auth_details',
        return_value={'user': {'rootFolderId': uuid}},
    )


def _stub_get_folder_content(tree):
    """tree is a dict {folder_uuid: {'folders': [...], 'files': [...]}}.

    Returns a context manager patching drive_service.get_folder_content
    AND clearing the in-memory cache so previous tests don't leak.
    """
    drive_service.folder_content_cache.clear()

    def fake(uuid):
        return tree.get(uuid, {'folders': [], 'files': []})

    return patch.object(drive_service, 'get_folder_content', side_effect=fake)


# ---------- resolve_path ----------

def test_resolve_root_returns_root_metadata():
    with _set_root():
        out = drive_service.resolve_path('/')
    assert out['type'] == 'folder'
    assert out['uuid'] == 'root-uuid'
    assert out['path'] == '/'


def test_resolve_dot_treated_as_root():
    with _set_root():
        out = drive_service.resolve_path('.')
    assert out['type'] == 'folder'
    assert out['uuid'] == 'root-uuid'


def test_resolve_top_level_folder():
    tree = {
        'root-uuid': {
            'folders': [{'uuid': 'docs-uuid', 'plainName': 'Documents'}],
            'files': [],
        },
    }
    with _set_root(), _stub_get_folder_content(tree):
        out = drive_service.resolve_path('/Documents')
    assert out['type'] == 'folder'
    assert out['uuid'] == 'docs-uuid'
    assert out['path'] == '/Documents'


def test_resolve_nested_folder():
    tree = {
        'root-uuid': {'folders': [{'uuid': 'a', 'plainName': 'A'}], 'files': []},
        'a': {'folders': [{'uuid': 'b', 'plainName': 'B'}], 'files': []},
        'b': {'folders': [{'uuid': 'c', 'plainName': 'C'}], 'files': []},
    }
    with _set_root(), _stub_get_folder_content(tree):
        out = drive_service.resolve_path('/A/B/C')
    assert out['uuid'] == 'c'
    assert out['path'] == '/A/B/C'


def test_resolve_file_with_extension():
    tree = {
        'root-uuid': {
            'folders': [],
            'files': [{'uuid': 'fid', 'plainName': 'report', 'type': 'pdf'}],
        },
    }
    with _set_root(), _stub_get_folder_content(tree):
        out = drive_service.resolve_path('/report.pdf')
    assert out['type'] == 'file'
    assert out['uuid'] == 'fid'
    assert out['path'] == '/report.pdf'


def test_resolve_file_without_extension():
    tree = {
        'root-uuid': {
            'folders': [],
            'files': [{'uuid': 'fid', 'plainName': 'README', 'type': ''}],
        },
    }
    with _set_root(), _stub_get_folder_content(tree):
        out = drive_service.resolve_path('/README')
    assert out['type'] == 'file'
    assert out['uuid'] == 'fid'


def test_resolve_missing_path_raises_filenotfound():
    tree = {'root-uuid': {'folders': [], 'files': []}}
    with _set_root(), _stub_get_folder_content(tree):
        with pytest.raises(FileNotFoundError):
            drive_service.resolve_path('/nope')


def test_resolve_partial_match_then_missing_raises():
    tree = {
        'root-uuid': {'folders': [{'uuid': 'a', 'plainName': 'A'}], 'files': []},
        'a': {'folders': [], 'files': []},
    }
    with _set_root(), _stub_get_folder_content(tree):
        with pytest.raises(FileNotFoundError) as ei:
            drive_service.resolve_path('/A/missing')
    # Error must include the path that failed
    assert 'missing' in str(ei.value)


def test_resolve_falls_back_to_legacy_name_field():
    """Some API responses use 'name' instead of 'plainName'."""
    tree = {
        'root-uuid': {
            'folders': [{'uuid': 'legacy', 'name': 'OldStyleFolder'}],
            'files': [],
        },
    }
    with _set_root(), _stub_get_folder_content(tree):
        out = drive_service.resolve_path('/OldStyleFolder')
    assert out['uuid'] == 'legacy'


def test_file_with_same_name_as_folder_prefers_folder_when_not_last():
    """If 'X' is both a folder and a file at the same level, traversal needs
    to follow the folder when X is an intermediate path component."""
    tree = {
        'root-uuid': {
            'folders': [{'uuid': 'fold', 'plainName': 'X'}],
            'files': [{'uuid': 'file', 'plainName': 'X', 'type': ''}],
        },
        'fold': {'folders': [], 'files': [{'uuid': 'leaf', 'plainName': 'leaf', 'type': 'txt'}]},
    }
    with _set_root(), _stub_get_folder_content(tree):
        out = drive_service.resolve_path('/X/leaf.txt')
    assert out['uuid'] == 'leaf'


# ---------- create_folder_recursive ----------

def test_create_folder_recursive_returns_root_for_empty_path():
    with _set_root():
        out = drive_service.create_folder_recursive('/')
    assert out['uuid'] == 'root-uuid'


def test_create_folder_recursive_existing_no_api_call():
    """If the path exists, no folder-creation should be requested."""
    tree = {
        'root-uuid': {'folders': [{'uuid': 'a', 'plainName': 'A'}], 'files': []},
    }
    with _set_root(), \
         _stub_get_folder_content(tree), \
         patch.object(drive_service, 'create_folder') as mock_create_top, \
         patch.object(drive_service.api, 'create_folder') as mock_create_api:
        out = drive_service.create_folder_recursive('/A')
    assert out['uuid'] == 'a'
    mock_create_top.assert_not_called()
    mock_create_api.assert_not_called()


def test_create_folder_recursive_creates_missing_parts():
    """All parts (intermediate + final) go through drive_service.create_folder
    so the parent cache is updated for each. Only the final part gets
    timestamps applied.

    Regression: prior implementation called api.create_folder directly for
    intermediate parts, which bypassed the parent-cache update and made
    subsequent resolve_path() calls fail with FileNotFoundError when reading
    from a stale root cache. Caught by tests/test_live_smoke.py.
    """
    tree = {'root-uuid': {'folders': [], 'files': []}}

    def fake_create(name, parent_uuid, creation_time=None, modification_time=None):
        return {
            'uuid': f"new-{name}", 'plainName': name,
            'parentFolderUuid': parent_uuid,
            'creationTime': creation_time, 'modificationTime': modification_time,
        }

    with _set_root(), \
         _stub_get_folder_content(tree), \
         patch.object(drive_service.api, 'create_folder') as mock_api_create, \
         patch.object(drive_service, 'create_folder',
                      side_effect=fake_create) as mock_create:
        out = drive_service.create_folder_recursive(
            '/A/B/C',
            creation_time='2026-01-01T00:00:00Z',
            modification_time='2026-01-02T00:00:00Z',
        )

    # All 3 parts go through self.create_folder (which keeps the parent
    # cache in sync). The raw api.create_folder is NOT called directly.
    mock_api_create.assert_not_called()
    assert mock_create.call_count == 3
    names_in_order = [call.args[0] for call in mock_create.call_args_list]
    assert names_in_order == ['A', 'B', 'C']

    # Only the LAST call (for 'C') gets timestamps; intermediates pass None.
    intermediate_call = mock_create.call_args_list[0]
    assert intermediate_call.kwargs.get('creation_time') is None
    assert intermediate_call.kwargs.get('modification_time') is None

    final_call = mock_create.call_args_list[-1]
    assert final_call.args[0] == 'C'
    assert final_call.kwargs.get('creation_time') == '2026-01-01T00:00:00Z'
    assert final_call.kwargs.get('modification_time') == '2026-01-02T00:00:00Z'

    # Returned info points at the final folder
    assert out['uuid'] == 'new-C'


def test_create_folder_recursive_handles_already_exists_race():
    """If create_folder reports 'already exists' (race vs. concurrent client),
    fall back to resolve_path to discover the existing UUID."""
    tree = {
        # On the lookup we don't see A yet (stale cache), but resolve_path
        # below WILL find it once a fresh content fetch happens.
        'root-uuid': {'folders': [], 'files': []},
    }

    def fake_api_create(payload):
        raise Exception("Folder already exists at parent")

    def fake_top_create(name, parent_uuid, creation_time=None, modification_time=None):
        raise Exception("Folder already exists at parent")

    def fake_resolve(path):
        # Resolve discovers the folder created by the racing client.
        assert path == '/A'
        return {'type': 'folder', 'uuid': 'race-uuid', 'metadata': {'uuid': 'race-uuid'}}

    with _set_root(), \
         _stub_get_folder_content(tree), \
         patch.object(drive_service.api, 'create_folder', side_effect=fake_api_create), \
         patch.object(drive_service, 'create_folder', side_effect=fake_top_create), \
         patch.object(drive_service, 'resolve_path', side_effect=fake_resolve):
        out = drive_service.create_folder_recursive('/A')

    assert out['uuid'] == 'race-uuid'


# ---------- folder content caching ----------

def test_get_folder_content_caches_results():
    """Second call must not re-hit the API within the cache TTL."""
    drive_service.folder_content_cache.clear()
    fake_folders = [{'uuid': 'cached-f', 'plainName': 'Cached'}]
    fake_files = []

    with _set_root(), \
         patch.object(drive_service, '_get_all_folders',
                      return_value=fake_folders) as mock_folders, \
         patch.object(drive_service, '_get_all_files',
                      return_value=fake_files):
        a = drive_service.get_folder_content('parent-1')
        b = drive_service.get_folder_content('parent-1')

    assert a == b
    # Only the first call should have hit _get_all_folders.
    assert mock_folders.call_count == 1
    drive_service.folder_content_cache.clear()
