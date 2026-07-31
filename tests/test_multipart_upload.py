"""Tests for the streaming + multipart large-file upload path.

The Internxt network API supports true S3 multipart
(``POST /v2/buckets/{id}/files/start?multiparts=N``) for files >= 100 MiB,
returning one pre-signed PUT URL per part plus an ``UploadId``; smaller files
get a single pre-signed URL. The CLI stream-encrypts the file with one
continuous AES-256-CTR keystream (so RAM is bounded by the part size, not the
file size) and stores ``ripemd160(sha256(ciphertext))`` as the shard hash.

These tests verify URL construction, the single-part vs multipart branch, the
finish payload shape, and that the hash matches the real protocol.
"""
import hashlib
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from services.drive import drive_service
from services.crypto import crypto_service, ripemd160


# ---------- api.start_upload honours the multiparts query param ----------

def test_start_upload_single_part_url():
    """parts=1 → multiparts=1 in the URL."""
    with patch.object(drive_service.api, '_make_request') as mock_req:
        fake_resp = MagicMock()
        fake_resp.content = True
        fake_resp.json.return_value = {'uploads': [{'index': 0, 'url': 'u', 'uuid': 'x'}]}
        mock_req.return_value = fake_resp

        drive_service.api.start_upload('bucket', 50_000, auth=('u', 'p'))

        assert 'multiparts=1' in mock_req.call_args[0][1]


def test_start_upload_multipart_url():
    """parts>1 → multiparts=N in the URL (multipart is now supported)."""
    with patch.object(drive_service.api, '_make_request') as mock_req:
        fake_resp = MagicMock()
        fake_resp.content = True
        fake_resp.json.return_value = {'uploads': [{'index': 0, 'urls': [], 'uuid': 'x'}]}
        mock_req.return_value = fake_resp

        drive_service.api.start_upload('bucket', 200_000_000, auth=('u', 'p'), parts=7)

        assert 'multiparts=7' in mock_req.call_args[0][1]


# ---------- shared upload harness (real crypto) ----------

def _run_upload(file_size, part_size=30 * 1024 * 1024, multipart_min=100 * 1024 * 1024):
    """Drive a real upload through _perform_network_upload with a mocked API,
    returning (captured, plaintext)."""
    data = os.urandom(file_size)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.bin')
    tmp.write(data)
    tmp.close()

    import math
    use_multipart = file_size >= multipart_min
    parts = math.ceil(file_size / part_size) if use_multipart else 1

    captured = {'chunk_calls': 0, 'part_urls': []}

    def fake_start_upload(bucket_id, size, auth, parts=1, chunk_size=0):
        captured['size'] = size
        captured['parts_requested'] = parts
        if parts > 1:
            return {'uploads': [{
                'uuid': 'net-uuid',
                'url': None,
                'urls': [f'https://part/{i}' for i in range(parts)],
                'UploadId': 'UPID',
            }]}
        return {'uploads': [{'uuid': 'net-uuid', 'url': 'https://single'}]}

    def fake_upload_part(url, payload, timeout):
        captured['part_urls'].append(url)
        return 'etag-' + url.rsplit('/', 1)[-1]

    def fake_upload_chunk(url, payload):
        captured['chunk_calls'] += 1
        captured['single_url'] = url

    def fake_finish(bucket_id, payload, auth):
        captured['finish'] = payload
        return {'id': 'NETFILE'}

    try:
        with tempfile.TemporaryDirectory() as data_dir, \
             patch.object(drive_service.config, 'internxt_cli_data_dir', Path(data_dir)), \
             patch.object(drive_service, 'UPLOAD_PART_SIZE', part_size), \
             patch.object(drive_service, 'MULTIPART_MIN_SIZE', multipart_min), \
             patch.object(drive_service, 'multipart_uploads', True), \
             patch.object(drive_service.api, 'start_upload', side_effect=fake_start_upload), \
             patch.object(drive_service.api, 'upload_part', side_effect=fake_upload_part), \
             patch.object(drive_service.api, 'upload_chunk', side_effect=fake_upload_chunk), \
             patch.object(drive_service.api, 'finish_upload', side_effect=fake_finish):
            fid = drive_service._perform_network_upload(
                Path(tmp.name), file_size, '00' * 12,
                ('abandon abandon abandon abandon abandon abandon '
                 'abandon abandon abandon abandon abandon about'),
                ('u', 'p'), 300)
    finally:
        os.unlink(tmp.name)

    captured['file_id'] = fid
    captured['expected_parts'] = parts
    captured['data'] = data
    return captured


def _expected_hash(data, index_hex):
    fk = crypto_service.generate_file_key('abandon abandon abandon abandon abandon abandon '
                                          'abandon abandon abandon abandon abandon about',
                                          '00' * 12, bytes.fromhex(index_hex))
    enc = Cipher(algorithms.AES(fk), modes.CTR(bytes.fromhex(index_hex)[:16])).encryptor()
    ct = enc.update(data) + enc.finalize()
    return ripemd160(hashlib.sha256(ct).digest()).hex()


# ---------- single-part path (small file) ----------

def test_small_file_uses_single_put():
    cap = _run_upload(50_000)
    assert cap['file_id'] == 'NETFILE'
    assert cap['parts_requested'] == 1
    assert cap['chunk_calls'] == 1
    assert cap['part_urls'] == []
    shard = cap['finish']['shards'][0]
    assert shard['uuid'] == 'net-uuid'
    assert 'UploadId' not in shard and 'parts' not in shard


def test_single_part_hash_is_ripemd160_sha256():
    cap = _run_upload(50_000)
    shard = cap['finish']['shards'][0]
    assert len(shard['hash']) == 40  # ripemd160, not 64-char sha256
    assert shard['hash'] == _expected_hash(cap['data'], cap['finish']['index'])


# ---------- multipart path (large file) ----------

def test_large_file_uses_multipart():
    # 250 KB file, 100 KB parts, 50 KB multipart floor → 3 parts
    cap = _run_upload(250_000, part_size=100_000, multipart_min=50_000)
    assert cap['parts_requested'] == 3
    assert cap['expected_parts'] == 3
    assert len(cap['part_urls']) == 3
    assert cap['chunk_calls'] == 0  # multipart never uses the single-PUT path


def test_multipart_finish_payload_shape():
    cap = _run_upload(250_000, part_size=100_000, multipart_min=50_000)
    shard = cap['finish']['shards'][0]
    assert shard['uuid'] == 'net-uuid'
    assert shard['UploadId'] == 'UPID'
    assert shard['parts'] == [
        {'PartNumber': 1, 'ETag': 'etag-0'},
        {'PartNumber': 2, 'ETag': 'etag-1'},
        {'PartNumber': 3, 'ETag': 'etag-2'},
    ]
    # One continuous CTR keystream → hash over the whole ciphertext, ripemd160(sha256)
    assert len(shard['hash']) == 40
    assert shard['hash'] == _expected_hash(cap['data'], cap['finish']['index'])
