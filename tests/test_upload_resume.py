"""Tests for resumable multipart uploads (issue #11).

The Internxt network API cannot recover an interrupted upload (files/start
always mints a new uuid/UploadId and there is no endpoint to re-issue
presigned URLs), but files/finish has no deadline and the AES-256-CTR
ciphertext is deterministic given the persisted file index. The CLI therefore
checkpoints multipart upload state (index, uuid, UploadId, presigned URLs,
completed ETags) to disk and, on a rerun of the same file, re-encrypts
locally while skipping the PUT for parts that already have an ETag.

These tests drive _perform_network_upload with a mocked network layer and
assert:
  - a successful upload leaves no checkpoint behind;
  - a failed part leaves a checkpoint from which a rerun genuinely resumes
    (no new files/start, only the missing part is PUT, byte-identical
    ciphertext, correct shard hash);
  - the in-session repair pass recovers transiently failed parts via a
    CTR-keystream-seeked re-encryption;
  - expired presigned URLs (HTTP 403) and a rejected files/finish fall back
    to a fresh upload;
  - --no-resume disables checkpointing.
"""
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from services.drive import drive_service
from services.crypto import crypto_service, ripemd160


MNEMONIC = ('abandon abandon abandon abandon abandon abandon '
            'abandon abandon abandon abandon abandon about')
BUCKET = '00' * 12

# 10 000 is a multiple of 16, like the production 30 MB part size, so the
# repair pass can seek the CTR keystream to any part boundary.
PART_SIZE = 10_000
MULTIPART_MIN = 50_000
FILE_SIZE = 60_000          # → 6 parts
PARTS = 6


def _encrypt_all(data: bytes, index_hex: str) -> bytes:
    """Reference encryption: the exact ciphertext the upload must produce."""
    fk = crypto_service.generate_file_key(MNEMONIC, BUCKET, bytes.fromhex(index_hex))
    enc = Cipher(algorithms.AES(fk), modes.CTR(bytes.fromhex(index_hex)[:16])).encryptor()
    return enc.update(data) + enc.finalize()


def _expected_hash(data: bytes, index_hex: str) -> str:
    return ripemd160(hashlib.sha256(_encrypt_all(data, index_hex)).digest()).hex()


class _Env:
    """A fake network + isolated data dir that persists across multiple
    _perform_network_upload calls, so interrupted-then-resumed uploads can be
    exercised end to end."""

    def __init__(self, tmp_path: Path, file_size: int = FILE_SIZE):
        self.data = os.urandom(file_size)
        self.file = tmp_path / 'big.bin'
        self.file.write_bytes(self.data)
        self.file_size = file_size
        self.data_dir = tmp_path / 'datadir'

        self.url_ns = 'https://fresh'      # namespace for newly issued URLs
        self.start_calls = []              # parts requested per files/start
        self.put_log = []                  # (url, payload bytes) of successful PUTs
        self.finish_calls = []             # files/finish payloads
        self.fail_urls = set()             # PUTs that fail permanently
        self.fail_countdown = {}           # url -> remaining failures (transient)
        self.expired_urls = set()          # PUTs that answer HTTP 403
        self.finish_errors = 0             # ValueErrors left to raise from finish
        self.on_put = None                 # optional hook(url)

    # --- network fakes ---

    def fake_start(self, bucket_id, size, auth, parts=1, chunk_size=0):
        self.start_calls.append(parts)
        n = len(self.start_calls)
        return {'uploads': [{
            'uuid': f'net-uuid-{n}',
            'urls': [f'{self.url_ns}/{i}' for i in range(parts)],
            'UploadId': f'UPID-{n}',
        }]}

    def fake_upload_part(self, url, payload, timeout):
        if self.on_put:
            self.on_put(url)
        if url in self.expired_urls:
            resp = requests.Response()
            resp.status_code = 403
            raise requests.exceptions.HTTPError('403 Forbidden', response=resp)
        if url in self.fail_urls:
            raise RuntimeError(f'connection dropped on {url}')
        if self.fail_countdown.get(url, 0) > 0:
            self.fail_countdown[url] -= 1
            raise RuntimeError(f'transient failure on {url}')
        self.put_log.append((url, bytes(payload)))
        return 'etag-' + url.rsplit('/', 1)[-1]

    def fake_finish(self, bucket_id, payload, auth):
        self.finish_calls.append(payload)
        if self.finish_errors > 0:
            self.finish_errors -= 1
            raise ValueError('API Error (409): upload not found')
        return {'id': 'NETFILE'}

    # --- driving the upload ---

    def run(self, resume: bool = True):
        real_retry = drive_service._put_with_retry

        def fast_retry(do_put, part_no, total_parts, max_retries=3):
            # One attempt per PUT → no backoff sleeps in tests. 403 handling
            # and error propagation stay identical to production.
            return real_retry(do_put, part_no, total_parts, max_retries=1)

        with patch.object(drive_service.config, 'internxt_cli_data_dir', self.data_dir), \
             patch.object(drive_service, 'UPLOAD_PART_SIZE', PART_SIZE), \
             patch.object(drive_service, 'MULTIPART_MIN_SIZE', MULTIPART_MIN), \
             patch.object(drive_service, 'multipart_uploads', True), \
             patch.object(drive_service, 'UPLOAD_REPAIR_DELAY', 0), \
             patch.object(drive_service, 'resume_uploads', resume), \
             patch.object(drive_service, '_put_with_retry', side_effect=fast_retry), \
             patch.object(drive_service.api, 'start_upload', side_effect=self.fake_start), \
             patch.object(drive_service.api, 'upload_part', side_effect=self.fake_upload_part), \
             patch.object(drive_service.api, 'finish_upload', side_effect=self.fake_finish):
            return drive_service._perform_network_upload(
                self.file, self.file_size, BUCKET, MNEMONIC, ('u', 'p'), 300)

    # --- checkpoint inspection ---

    def checkpoint_path(self) -> Path:
        with patch.object(drive_service.config, 'internxt_cli_data_dir', self.data_dir):
            return drive_service._upload_checkpoint_path(self.file, BUCKET)

    def checkpoint(self):
        return json.loads(self.checkpoint_path().read_text())


@pytest.fixture(autouse=True)
def _reset_mem_state():
    drive_service._mem_reserved = 0
    yield
    drive_service._mem_reserved = 0


# ---------- happy path ----------

def test_successful_upload_removes_checkpoint(tmp_path):
    env = _Env(tmp_path)
    seen_checkpoint = []
    env.on_put = lambda url: seen_checkpoint.append(env.checkpoint_path().exists())

    assert env.run() == 'NETFILE'
    # The checkpoint existed while parts were uploading...
    assert any(seen_checkpoint)
    # ...and is gone after a successful finish.
    assert not env.checkpoint_path().exists()
    shard = env.finish_calls[0]['shards'][0]
    assert len(shard['parts']) == PARTS
    assert shard['hash'] == _expected_hash(env.data, env.finish_calls[0]['index'])


# ---------- interruption → checkpoint → resume ----------

def test_failed_part_leaves_checkpoint_and_rerun_resumes(tmp_path):
    env = _Env(tmp_path)
    env.fail_urls = {f'{env.url_ns}/2'}  # part 3 dies, also in the repair pass

    with pytest.raises(Exception, match='re-running the same upload'):
        env.run()

    cp = env.checkpoint()
    assert '3' not in cp['etags']
    # Every other part was uploaded (directly or via the repair pass).
    assert set(cp['etags']) == {'1', '2', '4', '5', '6'}
    saved_index = cp['index']
    assert env.start_calls == [PARTS]

    # Connection is back: rerun the same upload.
    env.fail_urls = set()
    env.put_log = []
    assert env.run() == 'NETFILE'

    # Resume did NOT restart the upload: no second files/start, and only the
    # missing part was PUT — with byte-identical ciphertext (same index).
    assert env.start_calls == [PARTS]
    assert [u for u, _ in env.put_log] == [f'{env.url_ns}/2']
    expected_ct = _encrypt_all(env.data, saved_index)
    assert env.put_log[0][1] == expected_ct[2 * PART_SIZE:3 * PART_SIZE]

    finish = env.finish_calls[-1]
    assert finish['index'] == saved_index
    shard = finish['shards'][0]
    assert shard['parts'] == [
        {'PartNumber': i + 1, 'ETag': f'etag-{i}'} for i in range(PARTS)]
    assert shard['hash'] == _expected_hash(env.data, saved_index)
    assert not env.checkpoint_path().exists()


def test_resume_after_finish_interruption_reputs_nothing(tmp_path):
    """Crash between the last part and files/finish: the rerun re-hashes
    locally, PUTs nothing, and just finishes."""
    env = _Env(tmp_path)
    env.finish_errors = 1
    with pytest.raises(ValueError):
        env.run()
    assert set(env.checkpoint()['etags']) == {str(i) for i in range(1, PARTS + 1)}

    env.put_log = []
    assert env.run() == 'NETFILE'
    assert env.start_calls == [PARTS]   # still only the original files/start
    assert env.put_log == []            # nothing re-uploaded
    assert not env.checkpoint_path().exists()


# ---------- in-session repair pass ----------

def test_repair_pass_recovers_transient_failure(tmp_path):
    env = _Env(tmp_path)
    env.fail_countdown = {f'{env.url_ns}/4': 1}  # part 5 fails once, then works

    assert env.run() == 'NETFILE'
    assert env.start_calls == [PARTS]

    # The repaired part's ciphertext (regenerated via CTR keystream seek) is
    # byte-identical to the producer's output.
    index = env.finish_calls[0]['index']
    expected_ct = _encrypt_all(env.data, index)
    repaired = [p for u, p in env.put_log if u == f'{env.url_ns}/4']
    assert repaired == [expected_ct[4 * PART_SIZE:5 * PART_SIZE]]
    shard = env.finish_calls[0]['shards'][0]
    assert shard['hash'] == _expected_hash(env.data, index)
    assert not env.checkpoint_path().exists()


# ---------- stale server state falls back to a fresh upload ----------

def test_resume_with_expired_urls_falls_back_to_fresh(tmp_path):
    env = _Env(tmp_path)
    env.fail_urls = {f'{env.url_ns}/2'}
    with pytest.raises(Exception, match='re-running'):
        env.run()
    old_index = env.checkpoint()['index']

    # The checkpointed URL now answers 403; a rerun must restart from scratch
    # with freshly issued URLs.
    env.expired_urls = {'https://fresh/2'}
    env.fail_urls = set()
    env.url_ns = 'https://second'
    assert env.run() == 'NETFILE'

    assert env.start_calls == [PARTS, PARTS]           # fresh files/start happened
    finish = env.finish_calls[-1]
    assert finish['index'] != old_index                # new crypto identity
    assert finish['shards'][0]['UploadId'] == 'UPID-2'
    assert not env.checkpoint_path().exists()


def test_resume_with_rejected_finish_falls_back_to_fresh(tmp_path):
    """The bridge purged the uploads record: files/finish rejects the resumed
    state, so the rerun restarts fresh instead of failing forever."""
    env = _Env(tmp_path)
    env.finish_errors = 1
    with pytest.raises(ValueError):
        env.run()
    assert env.checkpoint_path().exists()

    env.finish_errors = 1                              # reject the RESUMED finish too
    assert env.run() == 'NETFILE'
    assert env.start_calls == [PARTS, PARTS]
    assert env.finish_calls[-1]['shards'][0]['UploadId'] == 'UPID-2'
    assert not env.checkpoint_path().exists()


# ---------- opt-out ----------

def test_no_resume_disables_checkpointing(tmp_path):
    env = _Env(tmp_path)
    env.fail_urls = {f'{env.url_ns}/2'}
    with pytest.raises(Exception, match='Multipart upload failed'):
        env.run(resume=False)
    assert not env.checkpoint_path().exists()


# ---------- crypto: seekable upload keystream ----------

def test_upload_encryptor_at_matches_full_stream():
    data = os.urandom(50_000)
    index_hex = os.urandom(32).hex()
    full = _encrypt_all(data, index_hex)
    for offset in (0, 16, PART_SIZE, 3 * PART_SIZE):
        enc = crypto_service.new_upload_encryptor_at(MNEMONIC, BUCKET, index_hex, offset)
        assert enc.update(data[offset:offset + PART_SIZE]) + enc.finalize() == \
            full[offset:offset + PART_SIZE]


def test_upload_encryptor_at_rejects_unaligned_offset():
    with pytest.raises(ValueError, match='16-byte aligned'):
        crypto_service.new_upload_encryptor_at(MNEMONIC, BUCKET, 'ab' * 32, 10)


def test_upload_cipher_from_index_reproduces_ciphertext():
    data = os.urandom(10_000)
    index_hex = os.urandom(32).hex()
    enc = crypto_service.new_upload_cipher_from_index(MNEMONIC, BUCKET, index_hex)
    assert enc.update(data) + enc.finalize() == _encrypt_all(data, index_hex)
