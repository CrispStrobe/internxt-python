"""Tests for InternxtDAVResource metadata accessors.

These exercise the pure helpers (no I/O, no network) that translate
Internxt drive metadata into the property values WsgiDAV expects.
"""
import time

from services.webdav_provider import InternxtDAVResource


def _resource(metadata):
    """Build a resource without invoking DAVNonCollection.__init__ (which
    needs a real wsgidav environ + provider)."""
    r = InternxtDAVResource.__new__(InternxtDAVResource)
    r.file_metadata = metadata
    r.path = '/test'
    r._upload_buffer = None
    return r


# ---------- get_content_length ----------

def test_content_length_from_metadata():
    r = _resource({'size': 12345})
    assert r.get_content_length() == 12345


def test_content_length_string_size_is_coerced():
    r = _resource({'size': '789'})
    assert r.get_content_length() == 789


def test_content_length_defaults_to_zero():
    r = _resource({})
    assert r.get_content_length() == 0


# ---------- get_content_type (mimetypes) ----------

def test_content_type_for_text_file():
    r = _resource({'plainName': 'note', 'type': 'txt'})
    assert r.get_content_type() == 'text/plain'


def test_content_type_for_pdf():
    r = _resource({'plainName': 'report', 'type': 'pdf'})
    assert r.get_content_type() == 'application/pdf'


def test_content_type_for_jpeg():
    r = _resource({'plainName': 'photo', 'type': 'jpg'})
    assert r.get_content_type() == 'image/jpeg'


def test_content_type_for_unknown_extension_falls_back_to_octet_stream():
    r = _resource({'plainName': 'data', 'type': 'xyzunknown'})
    assert r.get_content_type() == 'application/octet-stream'


def test_content_type_for_extensionless_file():
    r = _resource({'plainName': 'README', 'type': ''})
    assert r.get_content_type() == 'application/octet-stream'


# ---------- get_creation_date / get_last_modified ----------

def test_creation_date_parses_iso_with_z_suffix():
    r = _resource({'createdAt': '2024-06-15T12:34:56Z'})
    ts = r.get_creation_date()
    # 2024-06-15T12:34:56Z = 1718454896 epoch
    assert abs(ts - 1718454896) < 1


def test_creation_date_falls_back_to_now_on_garbage():
    r = _resource({'createdAt': 'not-a-date'})
    ts = r.get_creation_date()
    # Within ~5 seconds of now
    assert abs(ts - time.time()) < 5


def test_creation_date_falls_back_when_missing():
    r = _resource({})
    ts = r.get_creation_date()
    assert abs(ts - time.time()) < 5


def test_last_modified_prefers_modificationTime_over_updatedAt():
    r = _resource({
        'modificationTime': '2025-01-01T00:00:00Z',
        'updatedAt': '2020-01-01T00:00:00Z',
    })
    ts = r.get_last_modified()
    # 2025-01-01 = 1735689600
    assert abs(ts - 1735689600) < 1


def test_last_modified_falls_back_to_updatedAt():
    r = _resource({'updatedAt': '2024-12-31T23:59:59Z'})
    ts = r.get_last_modified()
    # 2024-12-31T23:59:59Z = 1735689599
    assert abs(ts - 1735689599) < 1


# ---------- get_etag ----------

def test_etag_includes_uuid_size_modified():
    r = _resource({
        'uuid': 'abcdef0123456789',
        'size': 1000,
        'modificationTime': '2025-01-01T00:00:00Z',
    })
    etag = r.get_etag()
    # Must include the first 8 chars of uuid + size
    assert etag.startswith('abcdef01-')
    assert etag.endswith('-1000')


def test_etag_changes_when_size_changes():
    a = _resource({'uuid': 'u' * 16, 'size': 100,
                   'modificationTime': '2025-01-01T00:00:00Z'})
    b = _resource({'uuid': 'u' * 16, 'size': 200,
                   'modificationTime': '2025-01-01T00:00:00Z'})
    assert a.get_etag() != b.get_etag()


def test_etag_changes_when_mtime_changes():
    a = _resource({'uuid': 'u' * 16, 'size': 100,
                   'modificationTime': '2025-01-01T00:00:00Z'})
    b = _resource({'uuid': 'u' * 16, 'size': 100,
                   'modificationTime': '2025-06-01T00:00:00Z'})
    assert a.get_etag() != b.get_etag()


def test_etag_no_quotes():
    """Regression: WsgiDAV adds its own quotes — the resource must NOT include them."""
    r = _resource({
        'uuid': 'u' * 16, 'size': 100,
        'modificationTime': '2025-01-01T00:00:00Z',
    })
    etag = r.get_etag()
    assert '"' not in etag
    assert "'" not in etag


# ---------- capability flags ----------

def test_resource_supports_required_dav_features():
    r = _resource({})
    assert r.support_etag() is True
    assert r.support_ranges() is True  # required for resumable downloads
    assert r.support_content_length() is True
    assert r.support_modified() is True
