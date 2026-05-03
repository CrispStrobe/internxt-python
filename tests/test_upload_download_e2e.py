"""End-to-end upload/download cycle tests with mocked network.

We mock every API call and the upload/download chunk transports — but
crypto runs for real. This catches:
  - Encryption/decryption protocol drift
  - Wrong field names in upload start/finish payloads
  - Cache invalidation after upload
  - Timestamp threading from metadata → server payload
  - Round-trip integrity (encrypt → upload → "server stores" → download → decrypt = original)
"""
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from services.drive import drive_service


@pytest.fixture
def fake_user_creds():
    """Realistic-looking credentials with valid mnemonic."""
    return {
        'token': 't', 'newToken': 'nt',
        'user': {
            'email': 'u@example.com',
            'userId': 'user-uuid-42',
            'rootFolderId': 'root-uuid',
            'bridgeUser': 'u@example.com',
            'bucket': '00' * 12,  # valid hex for crypto
            'mnemonic': ('abandon abandon abandon abandon abandon abandon '
                         'abandon abandon abandon abandon abandon about'),
        },
    }


@pytest.fixture
def server_state():
    """A tiny in-memory 'storage server' that records bytes uploaded
    and replays them on download."""
    storage = {}  # network_uuid -> bytes
    return storage


@pytest.fixture(autouse=True)
def _reset_drive_state():
    drive_service.folder_content_cache.clear()
    drive_service._mem_reserved = 0
    yield
    drive_service.folder_content_cache.clear()
    drive_service._mem_reserved = 0


# ---------- upload_file_to_folder full path ----------

def test_upload_round_trips_through_real_crypto(tmp_path, fake_user_creds, server_state):
    """The headline cycle: write a file locally, run upload_file_to_folder
    (which encrypts), capture the bytes that go to the upload URL, then
    feed them through download_file (which decrypts) and verify the bytes
    match the original."""
    # 1. Create a local file
    payload_bytes = b"hello internxt e2e " * 1024  # ~19 KB
    local_file = tmp_path / "doc.txt"
    local_file.write_bytes(payload_bytes)

    # 2. Set up "network" — capture upload, replay on download
    captured_index = {}

    def fake_start_upload(bucket_id, file_size, auth):
        return {'uploads': [{
            'index': 0, 'size': file_size,
            'url': 'https://upload-url.example/blob1',
            'uuid': 'network-uuid-1',
        }]}

    def fake_upload_chunk(upload_url, chunk_data):
        # The "server" stores the encrypted bytes keyed by what we're about to register.
        server_state['network-uuid-1'] = chunk_data

    def fake_finish_upload(bucket_id, payload, auth):
        captured_index['index'] = payload['index']
        captured_index['shards'] = payload['shards']
        return {'id': 'network-file-id-1'}

    captured_create = {}

    def fake_create_file_entry(payload):
        captured_create.update(payload)
        return {**payload, 'uuid': 'created-file-uuid'}

    def fake_get_download_links(bucket_id, file_id, auth):
        # The server uses what we stored above
        return {
            'shards': [{'url': 'https://download-url.example/blob1'}],
            'index': captured_index['index'],
        }

    def fake_download_chunk(download_url):
        return server_state['network-uuid-1']

    def fake_get_file_metadata(file_uuid):
        # After upload, this is what the server would return for the new file
        return {
            'uuid': file_uuid,
            'bucket': fake_user_creds['user']['bucket'],
            'fileId': 'network-file-id-1',
            'size': len(payload_bytes),
            'plainName': 'doc',
            'type': 'txt',
            'creationTime': captured_create.get('creationTime'),
            'modificationTime': captured_create.get('modificationTime'),
        }

    def fake_upload_chunk_with_progress(self, upload_url, chunk_data, timeout):
        fake_upload_chunk(upload_url, chunk_data)

    with patch.object(drive_service.auth, 'get_auth_details',
                      return_value=fake_user_creds), \
         patch.object(drive_service.api, 'start_upload', side_effect=fake_start_upload), \
         patch.object(drive_service, '_upload_chunk_with_progress',
                      side_effect=lambda url, data, t: fake_upload_chunk(url, data)), \
         patch.object(drive_service.api, 'finish_upload', side_effect=fake_finish_upload), \
         patch.object(drive_service.api, 'create_file_entry',
                      side_effect=fake_create_file_entry):
        result = drive_service.upload_file_to_folder(
            str(local_file), 'dest-folder-uuid',
            creation_time='2025-01-01T00:00:00Z',
            modification_time='2025-06-01T00:00:00Z',
        )

    # Upload succeeded
    assert result['uuid'] == 'created-file-uuid'
    # Server entry has the right name/type/size/timestamps
    assert captured_create['plainName'] == 'doc'
    assert captured_create['type'] == 'txt'
    assert captured_create['size'] == len(payload_bytes)
    assert captured_create['folderUuid'] == 'dest-folder-uuid'
    assert captured_create['creationTime'] == '2025-01-01T00:00:00Z'
    assert captured_create['modificationTime'] == '2025-06-01T00:00:00Z'
    assert captured_create['encryptVersion'] == 'Aes03'

    # Bytes on the wire are NOT plaintext (encryption actually happened)
    assert server_state['network-uuid-1'] != payload_bytes
    assert len(server_state['network-uuid-1']) == len(payload_bytes)  # AES-CTR same length

    # 3. Now run download_file with the same "server" — verify bytes match
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()

    with patch.object(drive_service.auth, 'get_auth_details',
                      return_value=fake_user_creds), \
         patch.object(drive_service.api, 'get_file_metadata',
                      side_effect=fake_get_file_metadata), \
         patch.object(drive_service.api, 'get_download_links',
                      side_effect=fake_get_download_links), \
         patch.object(drive_service.api, 'download_chunk',
                      side_effect=fake_download_chunk):
        out_path = drive_service.download_file('created-file-uuid', str(download_dir))

    # The downloaded file matches the original byte-for-byte
    downloaded_bytes = Path(out_path).read_bytes()
    assert downloaded_bytes == payload_bytes


def test_upload_invalidates_destination_folder_cache(tmp_path, fake_user_creds, server_state):
    """After upload, the destination folder's cached listing must be updated
    to include the new file (no stale list on next get_folder_content)."""
    local_file = tmp_path / "doc.txt"
    local_file.write_bytes(b"hello")

    # Pre-populate cache with an empty folder listing
    drive_service.folder_content_cache['dest-folder-uuid'] = (
        9999999999.0,  # far-future cache time
        {'folders': [], 'files': []},
    )

    def fake_start_upload(bucket_id, file_size, auth):
        return {'uploads': [{
            'index': 0, 'size': file_size, 'url': 'u', 'uuid': 'nu',
        }]}

    with patch.object(drive_service.auth, 'get_auth_details',
                      return_value=fake_user_creds), \
         patch.object(drive_service.api, 'start_upload', side_effect=fake_start_upload), \
         patch.object(drive_service, '_upload_chunk_with_progress'), \
         patch.object(drive_service.api, 'finish_upload',
                      return_value={'id': 'nfid'}), \
         patch.object(drive_service.api, 'create_file_entry',
                      return_value={'uuid': 'new-file-uuid', 'plainName': 'doc'}):
        drive_service.upload_file_to_folder(str(local_file), 'dest-folder-uuid')

    # Cache should now contain the new file
    cached_time, content = drive_service.folder_content_cache['dest-folder-uuid']
    new_files = [f.get('uuid') for f in content['files']]
    assert 'new-file-uuid' in new_files


def test_upload_rejects_oversized_file(tmp_path, fake_user_creds):
    """File over 20 GB must fail validation BEFORE any network call."""
    local_file = tmp_path / "huge.bin"
    local_file.write_bytes(b"x")  # actually small

    fake_stat = MagicMock()
    fake_stat.st_size = drive_service.TWENTY_GIGABYTES + 1
    # is_file() goes through stat → must produce a S_IFREG mode
    fake_stat.st_mode = 0o100644

    with patch.object(drive_service.auth, 'get_auth_details',
                      return_value=fake_user_creds), \
         patch('pathlib.Path.stat', return_value=fake_stat), \
         patch('pathlib.Path.is_file', return_value=True):
        with pytest.raises(ValueError, match="too large"):
            drive_service.upload_file_to_folder(str(local_file), 'dest-uuid')


def test_upload_missing_local_file_raises(tmp_path, fake_user_creds):
    with patch.object(drive_service.auth, 'get_auth_details',
                      return_value=fake_user_creds):
        with pytest.raises(FileNotFoundError):
            drive_service.upload_file_to_folder(
                str(tmp_path / "no-such-file.bin"), 'dest-uuid')


# ---------- download_file edge cases ----------

def test_download_writes_to_destination_dir_with_correct_filename(tmp_path, fake_user_creds, server_state):
    """When destination is a directory, the file is saved as
    <dest>/<plainName>.<type>."""
    payload = b"some content"
    # Encrypt the same way upload would, so download's decrypt yields it back.
    enc, idx_hex = drive_service.crypto.encrypt_stream_internxt_protocol(
        payload,
        fake_user_creds['user']['mnemonic'],
        fake_user_creds['user']['bucket'],
    )

    metadata = {
        'uuid': 'fid', 'bucket': fake_user_creds['user']['bucket'],
        'fileId': 'nid', 'size': len(payload),
        'plainName': 'README', 'type': '',  # extensionless file
    }

    download_dir = tmp_path / "out"
    download_dir.mkdir()

    with patch.object(drive_service.auth, 'get_auth_details',
                      return_value=fake_user_creds), \
         patch.object(drive_service.api, 'get_file_metadata', return_value=metadata), \
         patch.object(drive_service.api, 'get_download_links',
                      return_value={'shards': [{'url': 'u'}], 'index': idx_hex}), \
         patch.object(drive_service.api, 'download_chunk', return_value=enc):
        out_path = drive_service.download_file('fid', str(download_dir))

    assert Path(out_path).name == 'README'
    assert Path(out_path).read_bytes() == payload


def test_download_preserves_modification_time_when_requested(tmp_path, fake_user_creds):
    """preserve_timestamps=True with modificationTime → file mtime matches."""
    payload = b"z" * 100
    enc, idx_hex = drive_service.crypto.encrypt_stream_internxt_protocol(
        payload, fake_user_creds['user']['mnemonic'], fake_user_creds['user']['bucket'])

    metadata = {
        'uuid': 'fid', 'bucket': fake_user_creds['user']['bucket'],
        'fileId': 'nid', 'size': len(payload),
        'plainName': 'doc', 'type': 'txt',
        'modificationTime': '2024-01-15T12:00:00Z',
    }

    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with patch.object(drive_service.auth, 'get_auth_details',
                      return_value=fake_user_creds), \
         patch.object(drive_service.api, 'get_file_metadata', return_value=metadata), \
         patch.object(drive_service.api, 'get_download_links',
                      return_value={'shards': [{'url': 'u'}], 'index': idx_hex}), \
         patch.object(drive_service.api, 'download_chunk', return_value=enc):
        out_path = drive_service.download_file('fid', str(out_dir),
                                               preserve_timestamps=True)

    # 2024-01-15T12:00:00Z = 1705320000 epoch
    actual_mtime = Path(out_path).stat().st_mtime
    assert abs(actual_mtime - 1705320000) < 2  # within 2s


def test_download_truncates_decrypted_data_to_exact_size(tmp_path, fake_user_creds):
    """AES-CTR can over-decrypt if the encrypted data has trailing bytes;
    download_file must trim to metadata['size']."""
    payload = b"exactly this much data!"
    enc, idx_hex = drive_service.crypto.encrypt_stream_internxt_protocol(
        payload, fake_user_creds['user']['mnemonic'], fake_user_creds['user']['bucket'])

    metadata = {
        'uuid': 'fid', 'bucket': fake_user_creds['user']['bucket'],
        'fileId': 'nid', 'size': len(payload),
        'plainName': 'doc', 'type': 'txt',
    }

    out_dir = tmp_path / "out"
    out_dir.mkdir()

    with patch.object(drive_service.auth, 'get_auth_details',
                      return_value=fake_user_creds), \
         patch.object(drive_service.api, 'get_file_metadata', return_value=metadata), \
         patch.object(drive_service.api, 'get_download_links',
                      return_value={'shards': [{'url': 'u'}], 'index': idx_hex}), \
         patch.object(drive_service.api, 'download_chunk', return_value=enc):
        out_path = drive_service.download_file('fid', str(out_dir))

    assert Path(out_path).read_bytes() == payload
    assert Path(out_path).stat().st_size == len(payload)
