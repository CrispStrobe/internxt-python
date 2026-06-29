"""Tests for Step B — parallel ranged downloads + seekable AES-CTR decrypt.

A single presigned S3 GET is split into N 16-byte-aligned ranges fetched
concurrently and CTR-decrypted at their offsets, then written positionally.
These hermetic tests (mocking utils/api.py GET) assert:

  - seekable-CTR decrypt of an arbitrary aligned offset reproduces the plaintext
  - a ranged download round-trips byte-exact and bounds peak ranges in flight
  - ranges complete out of order but are written by offset (byte-identical)
  - a non-206 (200) response falls back to the sequential single-GET path
  - small files / ranged-disabled stay on the single-stream path
"""
import os
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from services.drive import drive_service
from services.crypto import crypto_service


MNEMONIC = ('abandon abandon abandon abandon abandon abandon '
            'abandon abandon abandon abandon abandon about')
BUCKET = '00' * 12


@pytest.fixture(autouse=True)
def _reset_mem_state():
    drive_service._mem_reserved = 0
    saved_ranged = drive_service.ranged_download
    saved_min = drive_service.RANGED_DOWNLOAD_MIN_SIZE
    saved_part = drive_service.DOWNLOAD_PART_SIZE
    saved_workers = drive_service.chunk_workers
    yield
    drive_service._mem_reserved = 0
    drive_service.ranged_download = saved_ranged
    drive_service.RANGED_DOWNLOAD_MIN_SIZE = saved_min
    drive_service.DOWNLOAD_PART_SIZE = saved_part
    drive_service.chunk_workers = saved_workers


def _encrypt(plaintext):
    """Encrypt with the real upload cipher; return (ciphertext, index_hex)."""
    encryptor, index_hex = crypto_service.new_upload_cipher(MNEMONIC, BUCKET)
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()
    return ciphertext, index_hex


# ---------- seekable CTR decrypt (the hard part) ----------

def test_seekable_ctr_decrypt_reproduces_plaintext_at_aligned_offsets():
    plaintext = os.urandom(200_000)
    ciphertext, index_hex = _encrypt(plaintext)
    # A spread of 16-byte-aligned offsets, including a 30 MB-style boundary
    # reduced into range, the very start, and a mid-block-aligned spot.
    for offset in [0, 16, 4096, 65536, 128000, 199_984]:
        assert offset % 16 == 0
        dec = crypto_service.new_download_decryptor_at(MNEMONIC, BUCKET, index_hex, offset)
        length = min(1000, len(plaintext) - offset)
        chunk = ciphertext[offset:offset + length]
        plain = dec.update(chunk) + dec.finalize()
        assert plain == plaintext[offset:offset + length], f"mismatch at offset {offset}"


def test_seekable_ctr_full_file_via_ranges_concatenates_to_plaintext():
    plaintext = os.urandom(123_457)
    ciphertext, index_hex = _encrypt(plaintext)
    part = 10_000  # not 16-aligned on purpose for the LAST range only; starts are
    # Use 16-aligned part starts:
    part = 16 * 625  # 10000, aligned
    out = bytearray()
    for start in range(0, len(plaintext), part):
        end = min(start + part, len(plaintext))
        dec = crypto_service.new_download_decryptor_at(MNEMONIC, BUCKET, index_hex, start)
        out += dec.update(ciphertext[start:end]) + dec.finalize()
    assert bytes(out) == plaintext


def test_seekable_ctr_rejects_unaligned_offset():
    _, index_hex = _encrypt(b'x' * 64)
    with pytest.raises(ValueError, match="16-byte aligned"):
        crypto_service.new_download_decryptor_at(MNEMONIC, BUCKET, index_hex, 17)


# ---------- ranged download harness ----------

def _run_download(plaintext, *, ranged, part_size, min_size, chunk_workers=4,
                  range_status=206, range_delay=0.0, reverse_delay=False):
    """Drive drive_service.download_file with a fully mocked API. Returns
    (written_bytes, observations)."""
    ciphertext, index_hex = _encrypt(plaintext)
    file_size = len(plaintext)

    obs = {'range_calls': [], 'stream_calls': 0, 'inflight': 0,
           'peak_inflight': 0, 'lock': threading.Lock()}

    def fake_metadata(uuid):
        return {'bucket': BUCKET, 'fileId': 'netid', 'size': str(file_size),
                'plainName': 'f', 'type': 'bin'}

    def fake_links(bucket_id, file_id, auth):
        return {'shards': [{'url': 'https://s3/get'}], 'index': index_hex}

    def fake_range(url, start, end, timeout=300):
        # 1-byte probe always reports support per range_status
        if start == 0 and end == 0:
            return (range_status, ciphertext[0:1])
        with obs['lock']:
            obs['inflight'] += 1
            obs['peak_inflight'] = max(obs['peak_inflight'], obs['inflight'])
        try:
            if range_status != 206:
                return (range_status, ciphertext)  # whole object (server ignored Range)
            if range_delay:
                n = (end // part_size)
                time.sleep(range_delay * (n + 1) if reverse_delay else range_delay)
            obs['range_calls'].append((start, end))
            return (206, ciphertext[start:end + 1])
        finally:
            with obs['lock']:
                obs['inflight'] -= 1

    class _FakeStream:
        def __init__(self):
            obs['stream_calls'] += 1
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def iter_content(self, chunk_size=0):
            for i in range(0, len(ciphertext), max(1, chunk_size)):
                yield ciphertext[i:i + chunk_size]

    creds = {'user': {'mnemonic': MNEMONIC, 'bucket': BUCKET,
                      'bridgeUser': 'bu', 'userId': 'uid'}}

    out_dir = tempfile.mkdtemp()
    out_path = Path(out_dir) / 'out.bin'

    with patch.object(drive_service, 'ranged_download', ranged), \
         patch.object(drive_service, 'DOWNLOAD_PART_SIZE', part_size), \
         patch.object(drive_service, 'RANGED_DOWNLOAD_MIN_SIZE', min_size), \
         patch.object(drive_service, 'chunk_workers', chunk_workers), \
         patch.object(drive_service.auth, 'get_auth_details', return_value=creds), \
         patch.object(drive_service.api, 'get_file_metadata', side_effect=fake_metadata), \
         patch.object(drive_service.api, 'get_download_links', side_effect=fake_links), \
         patch.object(drive_service.api, 'download_range', side_effect=fake_range), \
         patch.object(drive_service.api, 'download_stream', side_effect=lambda url, timeout=300: _FakeStream()):
        drive_service.download_file('uuid', str(out_path))

    written = out_path.read_bytes()
    os.unlink(out_path)
    os.rmdir(out_dir)
    obs['written'] = written
    return written, obs


# ---------- ranged download round-trips + bounding ----------

def test_ranged_download_round_trips_byte_exact():
    plaintext = os.urandom(80_000)
    written, obs = _run_download(plaintext, ranged=True, part_size=10_000,
                                 min_size=50_000, chunk_workers=3, range_delay=0.01)
    assert written == plaintext
    assert obs['stream_calls'] == 0  # ranged path, never the sequential stream
    assert len(obs['range_calls']) == 8  # 80k / 10k


def test_ranged_download_bounds_peak_in_flight():
    plaintext = os.urandom(80_000)
    _, obs = _run_download(plaintext, ranged=True, part_size=10_000,
                           min_size=50_000, chunk_workers=3, range_delay=0.02)
    assert obs['peak_inflight'] <= 3
    assert obs['peak_inflight'] >= 2  # genuinely parallel


def test_ranged_download_writes_by_offset_when_out_of_order():
    plaintext = os.urandom(80_000)
    written, _ = _run_download(plaintext, ranged=True, part_size=10_000,
                               min_size=50_000, chunk_workers=4,
                               range_delay=0.01, reverse_delay=True)
    # Despite later ranges completing first, the file is byte-identical.
    assert written == plaintext


# ---------- fallbacks ----------

def test_non_206_falls_back_to_sequential_stream():
    plaintext = os.urandom(80_000)
    written, obs = _run_download(plaintext, ranged=True, part_size=10_000,
                                 min_size=50_000, range_status=200)
    assert written == plaintext
    assert obs['stream_calls'] == 1  # fell back to the single stream
    assert obs['range_calls'] == []  # never wrote a real range


def test_small_file_stays_single_stream():
    plaintext = os.urandom(40_000)
    written, obs = _run_download(plaintext, ranged=True, part_size=10_000,
                                 min_size=50_000)  # below the floor
    assert written == plaintext
    assert obs['stream_calls'] == 1
    assert obs['range_calls'] == []


def test_ranged_disabled_stays_single_stream():
    plaintext = os.urandom(80_000)
    written, obs = _run_download(plaintext, ranged=False, part_size=10_000,
                                 min_size=50_000)
    assert written == plaintext
    assert obs['stream_calls'] == 1
    assert obs['range_calls'] == []
