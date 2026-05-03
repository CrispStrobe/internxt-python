"""Tests for InternxtDAVCollection (folder resource) and the supporting
WebDAVAPIClient helpers.

We bypass DAVCollection.__init__ (which needs a real wsgidav environ) by
constructing instances via __new__ and seeding the attributes directly.
"""
from unittest.mock import patch


from services.webdav_provider import (
    InternxtDAVCollection,
    InternxtDAVResource,
    WebDAVAPIClient,
)


class _FakeProvider:
    """Stand-in for a real wsgidav DAVProvider — never accessed in tests."""
    pass


_FAKE_ENVIRON = {'wsgidav.provider': _FakeProvider()}


def _collection(path='/', metadata=None):
    """Build a collection without invoking the wsgidav __init__."""
    c = InternxtDAVCollection.__new__(InternxtDAVCollection)
    c.path = path
    c.environ = _FAKE_ENVIRON
    c.folder_metadata = metadata or {}
    c.provider = None
    c._content_cache = None
    c._content_cached_time = 0.0
    c.CACHE_TIMEOUT = 300
    return c


# ---------- WebDAVAPIClient.mark_deleted / is_recently_deleted ----------

def test_mark_deleted_then_is_recently_deleted_true():
    api = WebDAVAPIClient()
    assert api.is_recently_deleted('/foo') is False
    api.mark_deleted('/foo')
    assert api.is_recently_deleted('/foo') is True


def test_mark_deleted_clears_after_cap():
    """Set cap is 100 — adding the 101st must clear (otherwise it grows unbounded)."""
    api = WebDAVAPIClient()
    for i in range(101):
        api.mark_deleted(f'/p{i}')
    # After clearing, only the most recent insertion remains (in some impls
    # nothing remains). Either way, the set must NOT contain 100+ items.
    assert len(api._deleted_items) <= 1


# ---------- get_member_names ----------

def test_get_member_names_returns_names_from_content():
    c = _collection('/')
    fake_content = {'names': ['file1.txt', 'folder1', 'file2.pdf']}
    with patch.object(c, '_get_content', return_value=fake_content):
        names = c.get_member_names()
    assert names == ['file1.txt', 'folder1', 'file2.pdf']


def test_get_member_names_returns_empty_list_on_error():
    c = _collection('/')
    with patch.object(c, '_get_content', side_effect=ConnectionError("net")):
        names = c.get_member_names()
    assert names == []


# ---------- get_member ----------

def test_get_member_returns_collection_for_subfolder():
    c = _collection('/Documents')
    fake_content = {
        'folders': [{'plainName': 'Reports', 'uuid': 'rep-uuid'}],
        'files': [],
    }
    with patch.object(c, '_get_content', return_value=fake_content):
        m = c.get_member('Reports')
    assert m is not None
    assert isinstance(m, InternxtDAVCollection)
    assert m.path == '/Documents/Reports'


def test_get_member_returns_resource_for_file():
    c = _collection('/Documents')
    fake_content = {
        'folders': [],
        'files': [{'plainName': 'note', 'type': 'txt', 'uuid': 'n-uuid'}],
    }
    with patch.object(c, '_get_content', return_value=fake_content):
        m = c.get_member('note.txt')
    assert m is not None
    assert isinstance(m, InternxtDAVResource)
    assert m.path == '/Documents/note.txt'


def test_get_member_handles_extensionless_file():
    c = _collection('/')
    fake_content = {
        'folders': [],
        'files': [{'plainName': 'README', 'type': '', 'uuid': 'r-uuid'}],
    }
    with patch.object(c, '_get_content', return_value=fake_content):
        m = c.get_member('README')
    assert m is not None
    assert isinstance(m, InternxtDAVResource)


def test_get_member_returns_none_when_not_found():
    c = _collection('/')
    with patch.object(c, '_get_content',
                      return_value={'folders': [], 'files': []}):
        assert c.get_member('ghost') is None


def test_get_member_root_path_builds_correct_child_path():
    """At root '/', child path must be '/name', not '//name'."""
    c = _collection('/')
    fake_content = {
        'folders': [{'plainName': 'Top', 'uuid': 'top-uuid'}],
        'files': [],
    }
    with patch.object(c, '_get_content', return_value=fake_content):
        m = c.get_member('Top')
    assert m.path == '/Top'
    assert '//' not in m.path


# ---------- create_empty_resource ----------

def test_create_empty_resource_with_extension():
    c = _collection('/Docs')
    r = c.create_empty_resource('newfile.txt')
    assert isinstance(r, InternxtDAVResource)
    assert r.path == '/Docs/newfile.txt'
    assert r.file_metadata['plainName'] == 'newfile'
    assert r.file_metadata['type'] == 'txt'
    assert r.file_metadata['uuid'].startswith('pending-')
    assert r.file_metadata['isUploading'] is True


def test_create_empty_resource_without_extension():
    c = _collection('/')
    r = c.create_empty_resource('README')
    assert r.file_metadata['plainName'] == 'README'
    assert r.file_metadata['type'] == ''


def test_create_empty_resource_at_root():
    c = _collection('/')
    r = c.create_empty_resource('top.txt')
    # No double slashes
    assert r.path == '/top.txt'
    assert '//' not in r.path


# ---------- _get_content cache ----------

def test_get_content_caches_within_timeout():
    """Second call within CACHE_TIMEOUT must not re-fetch."""
    c = _collection('/some/path')

    call_count = {'n': 0}
    def fake_drive_list(path):
        call_count['n'] += 1
        return {'folders': [], 'files': []}

    with patch('services.drive.drive_service.list_folder_with_paths',
               side_effect=fake_drive_list):
        a = c._get_content()
        b = c._get_content()

    assert a == b
    assert call_count['n'] == 1  # cache prevented second call


def test_get_content_recomputes_after_cache_expires():
    c = _collection('/some/path')
    c.CACHE_TIMEOUT = 0  # immediate expiry

    call_count = {'n': 0}
    def fake_drive_list(path):
        call_count['n'] += 1
        return {'folders': [], 'files': []}

    with patch('services.drive.drive_service.list_folder_with_paths',
               side_effect=fake_drive_list):
        c._get_content()
        c._get_content()

    assert call_count['n'] == 2
