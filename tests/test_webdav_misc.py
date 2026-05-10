"""Final WebDAV provider gaps: large-file timestamp extraction in end_write,
small-file timestamp extraction, get_member exception swallowing,
isolated session without token refresh.
"""
from unittest.mock import patch, MagicMock


from services.webdav_provider import (
    InternxtDAVResource,
    InternxtDAVCollection,
    StreamingFileUpload,
    WebDAVAPIClient,
    webdav_api,
)


def _resource(path='/x.txt', metadata=None, provider=None):
    r = InternxtDAVResource.__new__(InternxtDAVResource)
    r.path = path
    r.environ = {}
    r.file_metadata = metadata or {}
    r.provider = provider
    r._upload_buffer = None
    return r


def _collection(path='/'):
    c = InternxtDAVCollection.__new__(InternxtDAVCollection)
    c.path = path
    c.environ = {}
    c.folder_metadata = {}
    c.provider = None
    c._content_cache = None
    c._content_cached_time = 0.0
    c.CACHE_TIMEOUT = 300
    return c


# ---------- end_write large-file timestamp extraction ----------

def test_end_write_large_file_extracts_timestamps_when_preserve_on(tmp_path):
    """When preserve_timestamps=True and file is on disk, the temp file's
    mtime/ctime get read and threaded into upload_file_to_folder."""
    fake_provider = MagicMock()
    fake_provider.preserve_timestamps = True
    r = _resource(path='/big.bin', provider=fake_provider)

    # Build a buffer that's already on disk
    buf = StreamingFileUpload()
    buf._switch_to_disk()
    buf.write(b"X" * 1024)
    r._upload_buffer = buf

    fake_creds = {'user': {'rootFolderId': 'root-uuid'}}
    captured = {}
    def fake_upload(file_path, parent_uuid, plain_name, file_type=None,
                    creation_time=None, modification_time=None):
        captured['creation_time'] = creation_time
        captured['modification_time'] = modification_time
        return {'uuid': 'x'}

    with patch('services.auth.auth_service.get_auth_details',
               return_value=fake_creds), \
         patch('services.drive.drive_service.upload_file_to_folder',
               side_effect=fake_upload):
        r.end_write(with_errors=False)

    # Both ctime and mtime extracted (or fallback to st_ctime if no birthtime)
    assert captured['modification_time'] is not None
    assert captured['creation_time'] is not None
    # ISO format with timezone
    assert 'T' in captured['modification_time']
    assert captured['modification_time'].endswith('+00:00')
    buf.cleanup()


def test_end_write_small_file_extracts_timestamps_when_preserve_on(tmp_path):
    """Small (memory) buffers — temp file is created in tempfile.NamedTemporaryFile,
    timestamps are read from THAT temp file."""
    fake_provider = MagicMock()
    fake_provider.preserve_timestamps = True
    r = _resource(path='/small.txt', provider=fake_provider)

    buf = StreamingFileUpload()
    buf.write(b"small content")
    r._upload_buffer = buf

    fake_creds = {'user': {'rootFolderId': 'root-uuid'}}
    captured = {}
    def fake_upload(file_path, parent_uuid, plain_name, file_type=None,
                    creation_time=None, modification_time=None):
        captured['creation_time'] = creation_time
        captured['modification_time'] = modification_time
        return {'uuid': 'x'}

    with patch('services.auth.auth_service.get_auth_details',
               return_value=fake_creds), \
         patch('services.drive.drive_service.upload_file_to_folder',
               side_effect=fake_upload):
        r.end_write(with_errors=False)

    # Timestamps extracted from temp file
    assert captured['modification_time'] is not None
    assert 'T' in captured['modification_time']


def test_end_write_skips_timestamp_extraction_when_preserve_off():
    """preserve_timestamps=False → no timestamps in upload call."""
    fake_provider = MagicMock()
    fake_provider.preserve_timestamps = False
    r = _resource(path='/x.txt', provider=fake_provider)

    buf = StreamingFileUpload()
    buf.write(b"x")
    r._upload_buffer = buf

    fake_creds = {'user': {'rootFolderId': 'root-uuid'}}
    captured = {}
    def fake_upload(file_path, parent_uuid, plain_name, file_type=None,
                    creation_time=None, modification_time=None):
        captured['ct'] = creation_time
        captured['mt'] = modification_time
        return {'uuid': 'x'}

    with patch('services.auth.auth_service.get_auth_details',
               return_value=fake_creds), \
         patch('services.drive.drive_service.upload_file_to_folder',
               side_effect=fake_upload):
        r.end_write(with_errors=False)

    assert captured['ct'] is None
    assert captured['mt'] is None


# ---------- get_member exception path ----------

def test_get_member_returns_none_on_inner_exception():
    """If _get_content raises while iterating folders, get_member must
    return None instead of propagating."""
    c = _collection('/Documents')

    def boom(*a, **kw):
        raise RuntimeError("inner boom")

    with patch.object(c, '_get_content', side_effect=boom):
        out = c.get_member('something')
    assert out is None


# ---------- WebDAVAPIClient._get_isolated_session_without_token_refresh ----------

def test_isolated_session_without_refresh_creates_client():
    """The 'no-refresh' variant: builds a fresh client using current creds
    without calling auth_service.refresh_tokens (faster, used after a
    successful refresh has already happened)."""
    api = WebDAVAPIClient()
    api._thread_local.__dict__.pop('api_client', None)  # ensure fresh

    fake_creds = {'token': 't', 'newToken': 'nt', 'user': {'email': 'u'}}
    with patch('services.auth.auth_service.refresh_tokens') as mock_refresh, \
         patch('services.auth.auth_service.get_auth_details',
               return_value=fake_creds):
        client = api._get_isolated_session_without_token_refresh()

    assert client is not None
    # CRITICAL: must NOT call refresh_tokens
    mock_refresh.assert_not_called()


def test_isolated_session_without_refresh_thread_local_caches():
    api = WebDAVAPIClient()
    fake_creds = {'token': 't', 'newToken': 'nt', 'user': {'email': 'u'}}
    with patch('services.auth.auth_service.get_auth_details',
               return_value=fake_creds):
        a = api._get_isolated_session_without_token_refresh()
        b = api._get_isolated_session_without_token_refresh()
    assert a is b


# ---------- get_content error path → returns BytesIO with error message ----------

def test_get_content_writes_error_message_to_returned_bytesio():
    """When download fails, the BytesIO must contain a human-readable error string."""
    r = _resource(metadata={'uuid': 'real'})

    fake_creds = {'user': {
        'mnemonic': 'unused',
        'bridgeUser': 'u@example.com',
        'userId': 'u',
    }}
    with patch('services.auth.auth_service.get_auth_details',
               return_value=fake_creds), \
         patch.object(webdav_api, '_get_isolated_session',
                      side_effect=ConnectionError("Pipe broken")):
        out = r.get_content()
    body = out.read().decode('utf-8', errors='replace')
    assert 'error' in body.lower()
    assert 'Pipe broken' in body


# ---------- isolated session: 'no refresh' variant honors thread-local boundary ----------

def test_isolated_session_separate_threads_get_separate_clients():
    """Thread-local design: different threads get different clients.

    Both auth-service patches are hoisted out of the threaded body —
    `unittest.mock.patch` is not thread-safe, and races between
    `__enter__` / `__exit__` would let real auth code leak into one of
    the threads, raise MissingCredentialsError, and leave the
    `clients[2]` slot unpopulated → KeyError on the assertion.  Patching
    once for the whole test gives both threads a stable mocked auth
    surface for the entire window in which they call _get_isolated_session.
    """
    import threading

    api = WebDAVAPIClient()
    fake_creds = {'token': 't', 'newToken': 'nt', 'user': {'email': 'u'}}

    clients = {}
    errors = {}

    def grab_client(thread_id):
        try:
            clients[thread_id] = api._get_isolated_session()
        except Exception as e:  # surface, don't silently lose the slot
            errors[thread_id] = e

    with patch('services.auth.auth_service.get_auth_details',
               return_value=fake_creds), \
         patch('services.auth.auth_service.refresh_tokens'):
        t1 = threading.Thread(target=grab_client, args=(1,))
        t2 = threading.Thread(target=grab_client, args=(2,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

    # Surface in-thread errors so the failure is the actual cause, not KeyError.
    assert not errors, f"thread errors: {errors}"
    # Each thread got its own ApiClient
    assert clients[1] is not clients[2]
