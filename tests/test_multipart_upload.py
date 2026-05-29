"""Tests for upload-related API methods and large-file upload path.

The Internxt network API uses a single pre-signed URL per upload
(multiparts=1 in the query string). The start_upload method accepts
forward-compat params (parts, chunk_size) but always sends a single
upload entry. These tests verify correct URL construction and that
the existing single-part upload path handles files correctly.
"""
import hashlib
from unittest.mock import MagicMock, patch

from services.drive import drive_service


# ---------- api.start_upload always sends single part ----------

def test_start_upload_single_part_url():
    """start_upload always uses multiparts=1 in the URL."""
    with patch.object(drive_service.api, '_make_request') as mock_req:
        fake_resp = MagicMock()
        fake_resp.content = True
        fake_resp.json.return_value = {'uploads': [{'index': 0, 'url': 'u', 'uuid': 'x'}]}
        mock_req.return_value = fake_resp

        drive_service.api.start_upload('bucket', 50_000, auth=('u', 'p'))

        url_arg = mock_req.call_args[0][1]
        assert 'multiparts=1' in url_arg


def test_start_upload_ignores_parts_param():
    """Even when parts>1 is passed, the URL still uses multiparts=1."""
    with patch.object(drive_service.api, '_make_request') as mock_req:
        fake_resp = MagicMock()
        fake_resp.content = True
        fake_resp.json.return_value = {'uploads': [{'index': 0, 'url': 'u', 'uuid': 'x'}]}
        mock_req.return_value = fake_resp

        drive_service.api.start_upload('bucket', 200, auth=('u', 'p'),
                                        parts=3, chunk_size=64)

        url_arg = mock_req.call_args[0][1]
        assert 'multiparts=1' in url_arg


# ---------- upload_file_to_folder uses single upload ----------

def _mock_upload_flow(file_size):
    """Set up mocks for upload_file_to_folder and return captured data."""
    import tempfile
    import os

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.bin')
    tmp.write(b'\x42' * file_size)
    tmp.close()

    captured = {'upload_urls': [], 'shards': None}

    fake_encrypted = b'\xAB' * (file_size + 16)
    fake_index_hex = 'abcdef1234567890'

    def fake_encrypt(*args, **kwargs):
        return fake_encrypted, fake_index_hex

    def fake_start_upload(bucket_id, size, auth, parts=1, chunk_size=0):
        return {'uploads': [{'index': 0, 'url': 'https://upload/0', 'uuid': 'uuid-0'}]}

    def fake_upload_chunk(url, data, timeout):
        captured['upload_urls'].append(url)

    def fake_finish_upload(bucket_id, payload, auth):
        captured['shards'] = payload['shards']
        return {'id': 'net-file-id'}

    fake_created = {'uuid': 'created-uuid', 'plainName': 'test', 'type': 'bin'}

    try:
        with patch.object(drive_service, 'auth', MagicMock()) as mock_auth, \
             patch.object(drive_service, 'crypto', MagicMock()) as mock_crypto, \
             patch.object(drive_service.api, 'start_upload', side_effect=fake_start_upload), \
             patch.object(drive_service, '_upload_chunk_with_progress', side_effect=fake_upload_chunk), \
             patch.object(drive_service.api, 'finish_upload', side_effect=fake_finish_upload), \
             patch.object(drive_service.api, 'create_file_entry', return_value=fake_created), \
             patch.object(drive_service, '_mem_acquire'), \
             patch.object(drive_service, '_mem_release'), \
             patch.object(drive_service, '_available_memory', return_value=4 * 1024 * 1024 * 1024):

            mock_auth.get_auth_details.return_value = {
                'user': {'bucket': 'bkt', 'mnemonic': 'mnem', 'userId': 'uid', 'bridgeUser': 'bu'},
                'token': 'tok'
            }
            mock_crypto.encrypt_stream_internxt_protocol.side_effect = fake_encrypt
            drive_service.upload_file_to_folder(tmp.name, 'dest-folder-uuid')
    finally:
        os.unlink(tmp.name)

    return captured, fake_encrypted


def test_upload_uses_single_url():
    """All uploads use a single pre-signed URL."""
    captured, _ = _mock_upload_flow(200)
    assert len(captured['upload_urls']) == 1
    assert captured['upload_urls'][0] == 'https://upload/0'


def test_upload_sends_single_shard():
    """finish_upload receives exactly one shard with the full-data hash."""
    captured, encrypted = _mock_upload_flow(200)
    assert len(captured['shards']) == 1
    expected_hash = hashlib.sha256(encrypted).hexdigest()
    assert captured['shards'][0]['hash'] == expected_hash
    assert captured['shards'][0]['uuid'] == 'uuid-0'


def test_upload_large_file_same_path():
    """Files above MULTIPART_THRESHOLD still use the same single-upload path."""
    orig = drive_service.MULTIPART_THRESHOLD
    drive_service.MULTIPART_THRESHOLD = 50  # lower for test
    try:
        captured, encrypted = _mock_upload_flow(200)  # above threshold
    finally:
        drive_service.MULTIPART_THRESHOLD = orig

    assert len(captured['upload_urls']) == 1
    assert len(captured['shards']) == 1
