"""Tests for within-file multipart chunk concurrency (Step A).

A single large file (>= 100 MiB) is uploaded with its 30 MB parts PUT in
parallel through a bounded worker pool, while the AES-CTR keystream and the
content hash stay strictly sequential in the producer. These tests assert the
four non-negotiable constraints with a mocked network layer:

  1. SEQUENTIAL CRYPTO — the stored hash is identical regardless of how many
     part PUTs run concurrently (one continuous keystream).
  2. ORDER BY INDEX — parts_manifest is ordered by PartNumber even when parts
     finish out of order.
  3. BOUNDED IN FLIGHT — peak concurrent PUTs <= chunk_workers and peak bytes
     reserved via the memory gate <= chunk_workers * part_size.
  4. FAILURE — a failing part is surfaced after join.
"""
import hashlib
import math
import os
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from services.drive import drive_service
from services.crypto import crypto_service, ripemd160


MNEMONIC = ('abandon abandon abandon abandon abandon abandon '
            'abandon abandon abandon abandon abandon about')
BUCKET = '00' * 12


@pytest.fixture(autouse=True)
def _reset_mem_state():
    drive_service._mem_reserved = 0
    saved = drive_service.chunk_workers
    yield
    drive_service._mem_reserved = 0
    drive_service.chunk_workers = saved


def _run_multipart(file_size, part_size, multipart_min, chunk_workers,
                   part_delay=0.0, fail_index=None, reverse_delay=False):
    """Drive a real multipart upload through _perform_network_upload with a
    mocked network, returning a dict of observations."""
    data = os.urandom(file_size)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.bin')
    tmp.write(data)
    tmp.close()

    parts = math.ceil(file_size / part_size)

    obs = {
        'data': data,
        'parts': parts,
        'inflight': 0,
        'peak_inflight': 0,
        'peak_mem': 0,
        'lock': threading.Lock(),
    }

    def fake_start_upload(bucket_id, size, auth, parts=1, chunk_size=0):
        return {'uploads': [{
            'uuid': 'net-uuid',
            'urls': [f'https://part/{i}' for i in range(parts)],
            'UploadId': 'UPID',
        }]}

    def fake_upload_part(url, payload, timeout):
        idx = int(url.rsplit('/', 1)[-1])
        with obs['lock']:
            obs['inflight'] += 1
            obs['peak_inflight'] = max(obs['peak_inflight'], obs['inflight'])
        try:
            if fail_index is not None and idx == fail_index:
                raise RuntimeError(f"boom on part {idx}")
            if part_delay:
                # reverse_delay makes later parts finish first → out-of-order.
                d = part_delay * (parts - idx) if reverse_delay else part_delay
                time.sleep(d)
            return f'etag-{idx}'
        finally:
            with obs['lock']:
                obs['inflight'] -= 1

    def fake_finish(bucket_id, payload, auth):
        obs['finish'] = payload
        return {'id': 'NETFILE'}

    real_acquire = drive_service._mem_acquire
    real_release = drive_service._mem_release

    def tracking_acquire(need):
        real_acquire(need)
        with obs['lock']:
            obs['peak_mem'] = max(obs['peak_mem'], drive_service._mem_reserved)

    try:
        with tempfile.TemporaryDirectory() as data_dir, \
             patch.object(drive_service.config, 'internxt_cli_data_dir', Path(data_dir)), \
             patch.object(drive_service, 'UPLOAD_REPAIR_ROUNDS', 0), \
             patch.object(drive_service, 'UPLOAD_REPAIR_DELAY', 0), \
             patch.object(drive_service, 'UPLOAD_PART_SIZE', part_size), \
             patch.object(drive_service, 'MULTIPART_MIN_SIZE', multipart_min), \
             patch.object(drive_service, 'chunk_workers', chunk_workers), \
             patch.object(drive_service, '_mem_acquire', side_effect=tracking_acquire), \
             patch.object(drive_service, '_mem_release', side_effect=real_release), \
             patch.object(drive_service.api, 'start_upload', side_effect=fake_start_upload), \
             patch.object(drive_service.api, 'upload_part', side_effect=fake_upload_part), \
             patch.object(drive_service.api, 'finish_upload', side_effect=fake_finish):
            obs['file_id'] = drive_service._perform_network_upload(
                Path(tmp.name), file_size, BUCKET, MNEMONIC, ('u', 'p'), 300)
    finally:
        os.unlink(tmp.name)
    return obs


def _expected_hash(data, index_hex):
    fk = crypto_service.generate_file_key(MNEMONIC, BUCKET, bytes.fromhex(index_hex))
    enc = Cipher(algorithms.AES(fk), modes.CTR(bytes.fromhex(index_hex)[:16])).encryptor()
    ct = enc.update(data) + enc.finalize()
    return ripemd160(hashlib.sha256(ct).digest()).hex()


# ---------- peak parts in flight <= N ----------

def test_peak_parts_in_flight_bounded_by_chunk_workers():
    # 8 parts, pool of 3 → never more than 3 PUTs at once.
    obs = _run_multipart(file_size=80_000, part_size=10_000, multipart_min=50_000,
                         chunk_workers=3, part_delay=0.02)
    assert obs['parts'] == 8
    assert obs['peak_inflight'] <= 3
    assert obs['peak_inflight'] >= 2  # actually parallel, not accidentally serial


def test_single_worker_is_effectively_serial():
    obs = _run_multipart(file_size=80_000, part_size=10_000, multipart_min=50_000,
                         chunk_workers=1, part_delay=0.005)
    assert obs['peak_inflight'] == 1


# ---------- manifest ordered by PartNumber regardless of completion order ----------

def test_manifest_ordered_by_index_when_parts_finish_out_of_order():
    obs = _run_multipart(file_size=80_000, part_size=10_000, multipart_min=50_000,
                         chunk_workers=4, part_delay=0.01, reverse_delay=True)
    shard = obs['finish']['shards'][0]
    assert shard['parts'] == [
        {'PartNumber': i + 1, 'ETag': f'etag-{i}'} for i in range(obs['parts'])
    ]


# ---------- sequential crypto: hash unchanged under concurrency ----------

def test_hash_identical_under_concurrency():
    obs = _run_multipart(file_size=123_457, part_size=10_000, multipart_min=50_000,
                         chunk_workers=4, part_delay=0.005, reverse_delay=True)
    shard = obs['finish']['shards'][0]
    assert shard['UploadId'] == 'UPID'
    assert len(shard['hash']) == 40
    assert shard['hash'] == _expected_hash(obs['data'], obs['finish']['index'])
    assert obs['file_id'] == 'NETFILE'


# ---------- bytes in flight bounded by the memory gate ----------

def test_inflight_bytes_bounded_by_gate():
    part_size = 10_000
    workers = 3
    obs = _run_multipart(file_size=80_000, part_size=part_size, multipart_min=50_000,
                         chunk_workers=workers, part_delay=0.02)
    # The gate should never reserve more than workers * part_size at once.
    assert obs['peak_mem'] <= workers * part_size
    # And memory is fully released at the end.
    assert drive_service._mem_reserved == 0


# ---------- a failing part is surfaced after join ----------

def test_failing_part_raises_after_join():
    with pytest.raises(Exception, match="Multipart upload failed on part"):
        _run_multipart(file_size=80_000, part_size=10_000, multipart_min=50_000,
                       chunk_workers=4, part_delay=0.005, fail_index=5)
    # Memory gate is drained even on failure.
    assert drive_service._mem_reserved == 0
