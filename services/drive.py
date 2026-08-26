#!/usr/bin/env python3
"""
internxt_cli/services/drive.py
with path resolution
"""

import os
import sys
import json
import math
import hashlib
import fnmatch
import concurrent.futures
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from tqdm import tqdm
import time
import uuid
import threading

import requests


class UploadUrlExpiredError(Exception):
    """A presigned S3 upload URL answered 403 (expired).

    Retrying the same URL is pointless — the network API cannot re-issue URLs
    for an existing UploadId, so the caller must restart with a fresh
    files/start (discarding any resume checkpoint that referenced the URLs).
    """


class _ResumeStateInvalidError(Exception):
    """Server rejected resumed upload state (e.g. the uploads record was
    purged before files/finish). The checkpoint must be discarded and the
    upload restarted from scratch."""

try:
    from ..config.config import config_service
    from ..utils.api import api_client
    from .crypto import crypto_service
    from .auth import auth_service
except (ImportError, ValueError):
    current_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(current_dir)
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    from config.config import config_service
    from utils.api import api_client
    from services.crypto import crypto_service
    from services.auth import auth_service


class DriveService:
    """
    Extended Drive operations with path resolution and trash operations
    """

    def __init__(self):
        self.config = config_service
        self.api = api_client
        self.crypto = crypto_service
        self.auth = auth_service

        self.folder_content_cache = {}
        self.cache_lock = threading.Lock()
        # Cache TTL for folder content listings. Bumped from 10m to 1h so that
        # long-running batch uploads (where we walk a remote tree once at the
        # start and then upload thousands of files) don't re-list folders.
        self.CACHE_DURATION_SECONDS = 3600  # 1 hour

        self.TWENTY_GIGABYTES = 20 * 1024 * 1024 * 1024   # 20GB limit
        # Streaming-upload tuning (matches the official clients):
        # the network API rejects multipart for files < 100 MiB, and drive-web
        # uploads larger files in 30 MB parts (one continuous AES-CTR keystream
        # sliced into S3 parts). RAM use during upload is bounded by the part
        # size, not the file size.
        self.MULTIPART_MIN_SIZE = 100 * 1024 * 1024       # server multipart floor
        self.UPLOAD_PART_SIZE = 30 * 1024 * 1024          # 30MB parts
        self.MAX_MULTIPARTS = 10000                       # server part-count ceiling
        # Within-file concurrency: how many multipart part PUTs may be in
        # flight at once for a SINGLE large file. The CTR keystream + content
        # hash stay strictly sequential in the producer; only the network PUTs
        # run in parallel. Bytes in flight are additionally bounded by the
        # memory gate (_mem_acquire). Overridable per-run (cli --chunk-workers).
        # Serial is the gateway-safe default. Concurrent multipart PUTs remain
        # available as an explicit per-run opt-in via --chunk-workers N.
        self.chunk_workers = 1
        self.verbose = False                             # CLI --verbose timing diagnostics

        # --- Resumable multipart uploads (issue #11) ---
        # The network API cannot recover an interrupted upload (files/start
        # always mints a new uuid/UploadId and there is no URL re-issue
        # endpoint), but nothing forbids finishing one late: files/finish has
        # no deadline and AES-256-CTR ciphertext is deterministic given the
        # persisted file index. So for multipart files we checkpoint
        # {index, uuid, UploadId, urls, completed ETags} to disk and, on a
        # rerun of the same file, re-encrypt locally (cheap) while skipping
        # the PUT for parts that already have an ETag. Works until the
        # presigned URLs expire (server answers 403 → fresh restart).
        self.resume_uploads = True                        # cli --no-resume disables
        self.CHECKPOINT_MAX_AGE = 24 * 3600               # presigned URLs won't outlive this
        # In-session repair: parts that exhausted their per-PUT retries are
        # re-encrypted (CTR keystream seek) and re-PUT sequentially in up to
        # this many extra rounds before the upload is declared failed. Rounds
        # are separated by a flat delay so brief outages can pass.
        self.UPLOAD_REPAIR_ROUNDS = 2
        self.UPLOAD_REPAIR_DELAY = 15                     # seconds between repair rounds
        self.LOCAL_READ_CHUNK_SIZE = 8 * 1024 * 1024       # smaller reads avoid some device/driver edge cases
        self.LOCAL_READ_RETRIES = 3

        # Step B — parallel ranged downloads (opt-in, riskier). When enabled and
        # the file is large enough, a single presigned S3 GET is split into N
        # 16-byte-aligned ranges fetched concurrently and CTR-decrypted at their
        # offsets (AES-CTR is seekable). Falls back to the sequential
        # single-stream path if the server ignores Range (200 not 206) or the
        # file is small. Toggled per-run by cli `download --ranged`.
        self.ranged_download = False
        self.DOWNLOAD_PART_SIZE = 30 * 1024 * 1024        # 30MB ranges (16-aligned)
        # Only bother parallelising downloads above this size (one extra probe
        # round-trip isn't worth it for small files).
        self.RANGED_DOWNLOAD_MIN_SIZE = 100 * 1024 * 1024

        # Memory-gated concurrency: only allow as many simultaneous
        # read+encrypt operations as fit in available RAM.  The semaphore
        # value is computed lazily per-file based on current free memory.
        self._mem_lock = threading.Lock()      # protects _mem_reserved
        self._mem_reserved = 0                 # bytes currently claimed
        self._mem_cond = threading.Condition(self._mem_lock)

    @staticmethod
    def _available_memory() -> int:
        """Return available RAM in bytes (best-effort, cross-platform)."""
        try:
            import psutil
            return psutil.virtual_memory().available
        except ImportError:
            pass
        # Platform-specific fallbacks (no psutil)
        try:
            if sys.platform == 'darwin':
                import subprocess
                ps = int(subprocess.check_output(['sysctl', '-n', 'hw.pagesize']).strip())
                vm = subprocess.check_output(['vm_stat']).decode()
                free = spec = 0
                for line in vm.splitlines():
                    if 'Pages free' in line:
                        free = int(line.split(':')[1].strip().rstrip('.'))
                    elif 'Pages speculative' in line:
                        spec = int(line.split(':')[1].strip().rstrip('.'))
                return (free + spec) * ps
            elif sys.platform == 'win32':
                import ctypes
                class MEMORYSTATUSEX(ctypes.Structure):
                    _fields_ = [
                        ('dwLength', ctypes.c_ulong),
                        ('dwMemoryLoad', ctypes.c_ulong),
                        ('ullTotalPhys', ctypes.c_ulonglong),
                        ('ullAvailPhys', ctypes.c_ulonglong),
                        ('ullTotalPageFile', ctypes.c_ulonglong),
                        ('ullAvailPageFile', ctypes.c_ulonglong),
                        ('ullTotalVirtual', ctypes.c_ulonglong),
                        ('ullAvailVirtual', ctypes.c_ulonglong),
                        ('ullAvailExtendedVirtual', ctypes.c_ulonglong),
                    ]
                stat = MEMORYSTATUSEX(dwLength=ctypes.sizeof(MEMORYSTATUSEX))
                ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
                return stat.ullAvailPhys
            elif sys.platform.startswith('linux'):
                # Read from /proc/meminfo (Linux only — gated explicitly so
                # tests that patch sys.platform to a synthetic value can
                # exercise the 4 GB fallback below).
                with open('/proc/meminfo') as f:
                    for line in f:
                        if line.startswith('MemAvailable:'):
                            return int(line.split()[1]) * 1024  # kB -> bytes
        except Exception:
            pass
        # Last resort: assume 4 GB available
        return 4 * 1024 * 1024 * 1024

    def _mem_acquire(self, need: int) -> None:
        """Block until *need* bytes can be reserved without exceeding available RAM.

        We keep a safety margin of 1 GB so the rest of the process (and OS)
        still has breathing room.  If no other reservation is active AND
        available memory is too low, we still let one worker through so that
        progress is never deadlocked (the OS may reclaim caches/buffers).
        """
        SAFETY_MARGIN = 1 * 1024 * 1024 * 1024  # 1 GB

        with self._mem_cond:
            while True:
                avail = self._available_memory()
                headroom = max(0, avail - SAFETY_MARGIN)
                if need <= headroom - self._mem_reserved:
                    # Enough real memory for this reservation
                    self._mem_reserved += need
                    return
                if self._mem_reserved == 0:
                    # Nothing else reserved — let one through to avoid deadlock,
                    # even if the OS reports tight memory (caches may be reclaimable).
                    self._mem_reserved += need
                    return
                # Wait for another worker to release memory
                self._mem_cond.wait(timeout=5)  # re-check every 5s

    def _mem_release(self, amount: int) -> None:
        """Return a previous reservation."""
        with self._mem_cond:
            self._mem_reserved = max(0, self._mem_reserved - amount)
            self._mem_cond.notify_all()

    def _get_network_auth(self, user_creds: Dict[str, Any]) -> tuple:
        """Creates Basic Auth for Network API"""
        bridge_user = user_creds.get('bridgeUser')
        user_id = user_creds.get('userId')
        if not bridge_user or not user_id:
            raise ValueError("Missing network credentials")
        
        hashed_password = hashlib.sha256(str(user_id).encode()).hexdigest()
        return (bridge_user, hashed_password)

    # ========== STREAMING NETWORK UPLOAD ==========

    def _put_with_retry(self, do_put, part_no: int, total_parts: int, max_retries: int = 3):
        """Run a single PUT (returning its result) with exponential-backoff retries.

        Presigned URLs are reusable until they expire, so re-PUTting the same
        part on a transient failure is safe. Only the failing part is retried,
        which is the whole point of multipart for slow/flaky connections.
        A 403 means the presigned URL itself expired — retrying is pointless
        (the API cannot re-issue URLs), so that raises UploadUrlExpiredError
        immediately.
        """
        last_err: Optional[Exception] = None
        for attempt in range(max_retries):
            try:
                return do_put()
            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code == 403:
                    raise UploadUrlExpiredError(
                        f"Part {part_no}/{total_parts}: presigned upload URL expired (HTTP 403)"
                    ) from e
                last_err = e
            except Exception as e:
                last_err = e
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                print(f"\n        ⚠️  Part {part_no}/{total_parts} failed "
                      f"(attempt {attempt + 1}/{max_retries}): {last_err}; retrying in {wait}s...")
                time.sleep(wait)
        raise Exception(f"Part {part_no}/{total_parts} failed after {max_retries} attempts: {last_err}")

    def _read_upload_chunk(self, f, file_path: Path, offset: int,
                           size: int, file_size: int) -> bytes:
        """Read an upload part with small local reads and retry diagnostics."""
        data = bytearray()
        while len(data) < size:
            absolute_offset = offset + len(data)
            read_size = min(self.LOCAL_READ_CHUNK_SIZE, size - len(data))
            last_err: Optional[OSError] = None
            piece = b''
            for attempt in range(self.LOCAL_READ_RETRIES):
                try:
                    f.seek(absolute_offset)
                    piece = f.read(read_size)
                    break
                except OSError as e:
                    last_err = e
                    if attempt < self.LOCAL_READ_RETRIES - 1:
                        wait = 2 ** attempt
                        print(f"\n        ⚠️  Local read failed for {file_path} "
                              f"at offset {absolute_offset}/{file_size} "
                              f"(attempt {attempt + 1}/{self.LOCAL_READ_RETRIES}): "
                              f"{e}; retrying in {wait}s...")
                        time.sleep(wait)
            else:
                raise IOError(
                    f"Local read failed for {file_path} at offset "
                    f"{absolute_offset}/{file_size}: {last_err}"
                )
            if not piece:
                raise IOError(
                    f"Unexpected EOF reading {file_path} at offset "
                    f"{absolute_offset}/{file_size}"
                )
            data.extend(piece)
        return bytes(data)

    @staticmethod
    def _is_transient_api_error(exc: BaseException) -> bool:
        msg = str(exc).lower()
        return (
            isinstance(exc, ConnectionError)
            or 'http 429' in msg
            or 'http 502' in msg
            or 'http 503' in msg
            or 'http 504' in msg
            or 'bad gateway' in msg
            or 'gateway timeout' in msg
            or 'temporarily unavailable' in msg
        )

    def _call_api_with_retry(self, call, label: str, max_retries: int = 4):
        """Retry transient Internxt API failures around non-S3 upload steps."""
        last_err: Optional[BaseException] = None
        for attempt in range(max_retries):
            try:
                return call()
            except Exception as e:
                if not self._is_transient_api_error(e):
                    raise
                last_err = e
                if attempt < max_retries - 1:
                    wait = 2 ** attempt
                    print(f"        ⚠️  {label} failed "
                          f"(attempt {attempt + 1}/{max_retries}): {e}; "
                          f"retrying in {wait}s...")
                    time.sleep(wait)
        raise Exception(f"{label} failed after {max_retries} attempts: {last_err}")

    # ---------- resumable-upload checkpoints ----------

    def _upload_checkpoint_path(self, file_path: Path, bucket_id: str) -> Path:
        """Checkpoint file for (file path, bucket) — deterministic so a rerun
        of the same upload finds the previous attempt's state."""
        checkpoint_dir = self.config.internxt_cli_data_dir / 'upload_checkpoints'
        cp_id = hashlib.sha256(
            f"{Path(file_path).resolve()}|{bucket_id}".encode()).hexdigest()[:32]
        return checkpoint_dir / f"{cp_id}.json"

    def _save_upload_checkpoint(self, cp_path: Path, data: Dict[str, Any]) -> None:
        """Atomically persist upload state. Best-effort: a checkpoint failure
        must never fail the upload itself."""
        try:
            cp_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = cp_path.with_suffix('.tmp')
            with open(tmp, 'w') as f:
                json.dump(data, f)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, cp_path)
        except Exception:
            pass

    def _load_upload_checkpoint(self, file_path: Path, bucket_id: str, file_size: int,
                                part_size: int, parts: int) -> Optional[Dict[str, Any]]:
        """Return a resumable checkpoint for this exact upload, or None.

        The checkpoint is only valid if the file is byte-identical to the
        interrupted attempt (size + mtime), the part layout matches (the
        recorded ETags are per-part), and it is younger than
        CHECKPOINT_MAX_AGE (presigned URLs expire server-side; stale
        checkpoints would just 403). Invalid checkpoints are deleted.
        """
        cp_path = self._upload_checkpoint_path(file_path, bucket_id)
        try:
            with open(cp_path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        try:
            mtime_ns = Path(file_path).stat().st_mtime_ns
        except OSError:
            return None
        valid = (
            data.get('version') == 1
            and data.get('file_size') == file_size
            and data.get('mtime_ns') == mtime_ns
            and data.get('bucket_id') == bucket_id
            and data.get('part_size') == part_size
            and data.get('parts') == parts
            and isinstance(data.get('urls'), list) and len(data['urls']) >= parts
            and bool(data.get('index')) and bool(data.get('uuid'))
            and data.get('upload_id') is not None
            and (time.time() - data.get('created', 0)) < self.CHECKPOINT_MAX_AGE
        )
        if not valid:
            self.remove_upload_checkpoint(str(cp_path))
            return None
        return data

    def remove_upload_checkpoint(self, checkpoint_file: str) -> None:
        """Remove an upload checkpoint (idempotent)."""
        try:
            Path(checkpoint_file).unlink()
        except Exception:
            pass

    def _prune_upload_checkpoints(self) -> None:
        """Delete checkpoints past CHECKPOINT_MAX_AGE (their presigned URLs
        are dead anyway). Called opportunistically when a new one is created."""
        checkpoint_dir = self.config.internxt_cli_data_dir / 'upload_checkpoints'
        try:
            for f in checkpoint_dir.glob('*.json'):
                try:
                    if time.time() - f.stat().st_mtime > self.CHECKPOINT_MAX_AGE:
                        f.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    def _perform_network_upload(self, file_path: Path, file_size: int, bucket_id: str,
                                mnemonic: str, network_auth: tuple,
                                timeout_seconds: int) -> str:
        """Stream-encrypt a file and upload it to the network; return the network file id.

        The file is encrypted with a single continuous AES-256-CTR keystream and
        hashed (sha256) incrementally as it is read, so only one part (~30 MB)
        is ever held in memory regardless of file size. Files >= 100 MiB are
        uploaded with true S3 multipart (one presigned PUT per part, each
        retried independently); smaller files use a single presigned PUT. The
        stored shard hash is ripemd160(sha256(ciphertext)), matching the
        official clients.

        Multipart uploads are RESUMABLE: state (file index, network uuid,
        UploadId, presigned URLs, completed ETags) is checkpointed to disk as
        parts finish, and a rerun of the same file skips already-uploaded
        parts (re-encrypting locally to recompute the shard hash — the CTR
        ciphertext is deterministic given the persisted index). Resume works
        until the presigned URLs expire; a 403 falls back to a fresh upload.
        """
        part_size = self.UPLOAD_PART_SIZE
        use_multipart = file_size >= self.MULTIPART_MIN_SIZE
        parts = 1
        if use_multipart:
            parts = math.ceil(file_size / part_size)
            if parts > self.MAX_MULTIPARTS:
                parts = self.MAX_MULTIPARTS
                # Keep part boundaries 16-byte aligned so the repair pass can
                # seek the CTR keystream to any part offset.
                part_size = math.ceil(file_size / parts / 16) * 16

        if use_multipart and self.resume_uploads:
            resume = self._load_upload_checkpoint(file_path, bucket_id, file_size,
                                                  part_size, parts)
            if resume is not None:
                done = len(resume.get('etags') or {})
                print(f"        🔁 Resuming interrupted upload: "
                      f"{done}/{parts} part(s) already uploaded")
                try:
                    return self._upload_via_network(
                        file_path, file_size, bucket_id, mnemonic, network_auth,
                        timeout_seconds, part_size, parts, use_multipart, resume)
                except (UploadUrlExpiredError, _ResumeStateInvalidError) as e:
                    print(f"        ⚠️  Resume no longer possible ({e}); "
                          f"restarting upload from scratch")
                    self.remove_upload_checkpoint(
                        str(self._upload_checkpoint_path(file_path, bucket_id)))

        try:
            return self._upload_via_network(
                file_path, file_size, bucket_id, mnemonic, network_auth,
                timeout_seconds, part_size, parts, use_multipart, None)
        except UploadUrlExpiredError as e:
            # Fresh URLs expired before the upload finished — the checkpoint
            # references dead URLs, so drop it rather than 403 again next run.
            if use_multipart and self.resume_uploads:
                self.remove_upload_checkpoint(
                    str(self._upload_checkpoint_path(file_path, bucket_id)))
            raise Exception(
                f"Upload failed: {e}. The presigned upload URLs expired before the "
                f"upload completed (connection too slow for the URL lifetime)."
            ) from e

    def _upload_via_network(self, file_path: Path, file_size: int, bucket_id: str,
                            mnemonic: str, network_auth: tuple, timeout_seconds: int,
                            part_size: int, parts: int, use_multipart: bool,
                            resume: Optional[Dict[str, Any]]) -> str:
        """One upload attempt (fresh or resumed). See _perform_network_upload."""
        resuming = resume is not None
        single_url = None
        urls: Optional[List[str]] = None
        upload_id = None
        done_etags: Dict[int, str] = {}

        if resume is not None:
            # Reuse the interrupted attempt's identity: same index → same
            # key/IV → byte-identical ciphertext, so already-PUT parts and the
            # recomputed shard hash stay consistent with what the server has.
            file_index_hex = resume['index']
            encryptor = self.crypto.new_upload_cipher_from_index(
                mnemonic, bucket_id, file_index_hex)
            file_network_uuid = resume['uuid']
            upload_id = resume['upload_id']
            urls = resume['urls']
            done_etags = {int(k): v for k, v in (resume.get('etags') or {}).items()}
        else:
            encryptor, file_index_hex = self.crypto.new_upload_cipher(mnemonic, bucket_id)
            if use_multipart:
                print(f"        Multipart upload: {parts} part(s) of "
                      f"{self._format_size(part_size)}")
            stage_started = time.perf_counter()
            start_response = self._call_api_with_retry(
                lambda: self.api.start_upload(
                    bucket_id, file_size, auth=network_auth, parts=parts),
                'files/start')
            if self.verbose:
                print(f"        [timing] files/start: {time.perf_counter() - stage_started:.3f}s")
            upload_details = start_response['uploads'][0]
            file_network_uuid = upload_details['uuid']
            if use_multipart:
                urls = upload_details.get('urls')
                upload_id = upload_details.get('UploadId') or upload_details.get('uploadId')
                if not urls or upload_id is None:
                    raise ValueError("Server did not return multipart URLs/UploadId for a multipart upload")
                if len(urls) < parts:
                    raise ValueError(f"Server returned {len(urls)} part URL(s), expected {parts}")
            else:
                single_url = upload_details.get('url')
                if not single_url:
                    raise ValueError("Server did not return an upload URL")

        sha = hashlib.sha256()

        # Checkpoint (multipart only): everything a later process needs to
        # finish this upload. Updated as each part's ETag comes back.
        checkpoint: Optional[Dict[str, Any]] = None
        checkpoint_path: Optional[Path] = None
        cp_lock = threading.Lock()
        if use_multipart and self.resume_uploads:
            checkpoint_path = self._upload_checkpoint_path(file_path, bucket_id)
            try:
                mtime_ns = file_path.stat().st_mtime_ns
            except OSError:
                mtime_ns = None
            checkpoint = {
                'version': 1,
                'file_path': str(file_path),
                'file_size': file_size,
                'mtime_ns': mtime_ns,
                'bucket_id': bucket_id,
                'part_size': part_size,
                'parts': parts,
                'index': file_index_hex,
                'uuid': file_network_uuid,
                'upload_id': upload_id,
                'urls': urls,
                'etags': {str(k): v for k, v in done_etags.items()},
                'created': resume['created'] if resume is not None else time.time(),
            }
            if not resuming:
                self._prune_upload_checkpoints()
                self._save_upload_checkpoint(checkpoint_path, checkpoint)

        # Multipart parts finish out of order, so we pre-size the manifest and
        # assign each ETag BY INDEX. The crypto (encryptor.update + sha.update)
        # stays strictly sequential in this producer thread; only the network
        # PUTs are dispatched to a bounded worker pool.
        n_workers = max(1, min(self.chunk_workers, parts)) if use_multipart else 1
        parts_manifest: List[Optional[Dict[str, Any]]] = [None] * parts
        bytes_done = 0

        def _record_etag(part_number: int, etag: str) -> None:
            parts_manifest[part_number - 1] = {'PartNumber': part_number, 'ETag': etag}
            if checkpoint is not None and checkpoint_path is not None:
                with cp_lock:
                    checkpoint['etags'][str(part_number)] = etag
                    self._save_upload_checkpoint(checkpoint_path, checkpoint)

        # Failures from worker threads are collected here; once one is seen the
        # producer stops DISPATCHING new PUTs but keeps reading/encrypting to
        # EOF so the cipher/hash state completes — undispatched parts are then
        # recovered by the repair pass (or a later resumed run).
        part_errors: List[Tuple[int, BaseException]] = []
        errors_lock = threading.Lock()
        # A 403 (expired presigned URL) is unrecoverable within this attempt;
        # it aborts dispatch and repair immediately.
        url_expired = threading.Event()
        # Cap parts in flight to n_workers (each ~part_size); _mem_acquire adds
        # the RAM ceiling on bytes in flight.
        inflight = threading.BoundedSemaphore(n_workers)

        def _put_part(idx: int, url: str, data: bytes) -> None:
            try:
                etag = self._put_with_retry(
                    lambda: self.api.upload_part(url, data, timeout_seconds),
                    idx + 1, parts)
                _record_etag(idx + 1, etag)
            except UploadUrlExpiredError as e:
                url_expired.set()
                with errors_lock:
                    part_errors.append((idx, e))
            except BaseException as e:  # noqa: BLE001 — surfaced by the producer after join
                with errors_lock:
                    part_errors.append((idx, e))
            finally:
                self._mem_release(len(data))
                inflight.release()

        executor = (concurrent.futures.ThreadPoolExecutor(max_workers=n_workers)
                    if use_multipart else None)
        dispatch = True
        try:
            with open(file_path, 'rb') as f, tqdm(
                    total=file_size, unit='B', unit_scale=True,
                    desc='        Resuming' if resuming else '        Uploading',
                    bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt} [{rate_fmt}] ETA: {remaining}',
                    leave=False) as pbar:
                for part_index in range(parts):
                    with errors_lock:
                        if part_errors:
                            dispatch = False
                    remaining = file_size - bytes_done
                    read_size = remaining if not use_multipart else min(part_size, remaining)
                    plaintext = self._read_upload_chunk(
                        f, file_path, bytes_done, read_size, file_size)
                    ciphertext = encryptor.update(plaintext)
                    sha.update(ciphertext)
                    bytes_done += len(plaintext)

                    if use_multipart:
                        assert urls is not None and executor is not None  # validated above
                        already = done_etags.get(part_index + 1)
                        if already is not None:
                            # Resumed part: ciphertext is identical to what was
                            # PUT last time (same index → same keystream), so
                            # only the hash needed recomputing.
                            parts_manifest[part_index] = {'PartNumber': part_index + 1,
                                                          'ETag': already}
                        elif dispatch and not url_expired.is_set():
                            # Reserve a slot (≤ n_workers in flight) and RAM for
                            # this part before handing the PUT off to a worker.
                            inflight.acquire()
                            self._mem_acquire(len(ciphertext))
                            executor.submit(_put_part, part_index, urls[part_index], ciphertext)
                        # else: leave manifest[part_index] None — the repair
                        # pass (or a resumed run) re-encrypts and re-PUTs it.
                    else:
                        self._put_with_retry(
                            lambda data=ciphertext: self.api.upload_chunk(single_url, data),
                            1, 1)
                    pbar.update(len(plaintext))
        finally:
            if executor is not None:
                executor.shutdown(wait=True)

        # AES-CTR has no padding, so finalize() is normally empty; fold it in anyway.
        tail = encryptor.finalize()
        if tail:
            sha.update(tail)

        if bytes_done != file_size:
            raise IOError(f"Read {bytes_done} bytes but file size is {file_size}")

        if use_multipart:
            assert urls is not None  # validated at start (fresh) or by the checkpoint (resume)
            missing = [i for i in range(parts) if parts_manifest[i] is None]
            if missing and not url_expired.is_set():
                missing = self._repair_missing_parts(
                    file_path, file_size, bucket_id, mnemonic, file_index_hex,
                    part_size, parts, urls, missing, timeout_seconds,
                    _record_etag, url_expired)
            if url_expired.is_set() and missing:
                raise UploadUrlExpiredError(
                    f"presigned URL(s) expired with {len(missing)} part(s) outstanding")
            if missing:
                with errors_lock:
                    part_errors.sort(key=lambda t: t[0])
                    first_err = part_errors[0][1] if part_errors else 'part(s) not uploaded'
                hint = (" Progress was checkpointed — re-running the same upload "
                        "will resume from the uploaded parts." if checkpoint is not None else "")
                raise Exception(
                    f"Multipart upload failed on part {missing[0] + 1}/{parts}: "
                    f"{first_err}.{hint}")

        if self.verbose:
            print(f"        [timing] parts complete; submitting files/finish")

        content_hash = self.crypto.shard_hash_from_sha256(sha.digest())
        shard: Dict[str, Any] = {'hash': content_hash, 'uuid': file_network_uuid}
        if use_multipart:
            shard['UploadId'] = upload_id
            shard['parts'] = parts_manifest
        finish_payload = {'index': file_index_hex, 'shards': [shard]}
        finish_started = time.perf_counter()
        try:
            finish_response = self._call_api_with_retry(
                lambda: self.api.finish_upload(bucket_id, finish_payload,
                                               auth=network_auth),
                'files/finish')
        except ValueError as e:
            # _make_request maps HTTP-status errors to ValueError. On a resumed
            # upload that means the server no longer accepts the old state
            # (e.g. the uploads record was purged) — restart fresh.
            if resuming:
                raise _ResumeStateInvalidError(str(e)) from e
            raise
        if self.verbose:
            print(f"        [timing] files/finish: {time.perf_counter() - finish_started:.3f}s")
        if checkpoint_path is not None:
            self.remove_upload_checkpoint(str(checkpoint_path))
        return finish_response['id']

    def _repair_missing_parts(self, file_path: Path, file_size: int, bucket_id: str,
                              mnemonic: str, file_index_hex: str, part_size: int,
                              parts: int, urls: List[str], missing: List[int],
                              timeout_seconds: int, record_etag, url_expired) -> List[int]:
        """In-session recovery: re-PUT parts that exhausted their retries.

        Each missing part's ciphertext is regenerated independently by seeking
        the CTR keystream to the part offset (deterministic — identical bytes
        to the producer's output), so nothing had to be kept in memory. Parts
        are retried sequentially, in up to UPLOAD_REPAIR_ROUNDS rounds spaced
        UPLOAD_REPAIR_DELAY apart, so brief outages can pass. Returns the
        parts still missing; sets ``url_expired`` and stops early on a 403.
        """
        for round_no in range(self.UPLOAD_REPAIR_ROUNDS):
            if not missing or url_expired.is_set():
                break
            time.sleep(self.UPLOAD_REPAIR_DELAY)
            print(f"        🔧 Repair round {round_no + 1}/{self.UPLOAD_REPAIR_ROUNDS}: "
                  f"retrying {len(missing)} failed part(s)...")
            still_missing: List[int] = []
            for pos, idx in enumerate(missing):
                offset = idx * part_size
                try:
                    with open(file_path, 'rb') as f:
                        plaintext = self._read_upload_chunk(
                            f, file_path, offset,
                            min(part_size, file_size - offset), file_size)
                    enc = self.crypto.new_upload_encryptor_at(
                        mnemonic, bucket_id, file_index_hex, offset)
                    ciphertext = enc.update(plaintext) + enc.finalize()
                    etag = self._put_with_retry(
                        lambda url=urls[idx], data=ciphertext:
                            self.api.upload_part(url, data, timeout_seconds),
                        idx + 1, parts)
                    record_etag(idx + 1, etag)
                except UploadUrlExpiredError:
                    url_expired.set()
                    still_missing.append(idx)
                    still_missing.extend(missing[pos + 1:])
                    return still_missing
                except Exception as e:
                    print(f"        ⚠️  Repair of part {idx + 1}/{parts} failed: {e}")
                    still_missing.append(idx)
            missing = still_missing
        return missing

    # ========== PATH RESOLUTION ==========

    def resolve_path(self, path: str) -> Dict[str, Any]:
        """
        Resolve a path to UUID and metadata
        Returns: {'type': 'file'/'folder', 'uuid': str, 'metadata': dict, 'path': str}
        """
        credentials = self.auth.get_auth_details()
        root_folder_uuid = credentials['user'].get('rootFolderId', '')
        
        path = path.strip()

        if path == '.':
            path = '/'

        if path.startswith('/'):
            path = path[1:]
        
        if not path:
            return {
                'type': 'folder', 'uuid': root_folder_uuid,
                'metadata': {'uuid': root_folder_uuid, 'plainName': 'Root'},
                'path': '/'
            }
        
        path_parts = [part for part in path.split('/') if part]
        current_folder_uuid = root_folder_uuid
        resolved_path_parts = []
        
        for i, part in enumerate(path_parts):
            is_last_part = (i == len(path_parts) - 1)
            folder_content = self.get_folder_content(current_folder_uuid)
            
            # Look for folder
            found_folder = None
            for folder in folder_content['folders']:
                if folder.get('plainName') == part or folder.get('name') == part:
                    found_folder = folder
                    break
            
            # Look for file (only if last part)
            found_file = None
            if is_last_part:
                for file in folder_content['files']:
                    file_name = file.get('plainName', '')
                    file_type = file.get('type', '')
                    full_name = f"{file_name}.{file_type}" if file_type else file_name
                    
                    if (file_name == part or full_name == part or file.get('name') == part):
                        found_file = file
                        break
            
            if found_folder and (not is_last_part or not found_file):
                resolved_path_parts.append(found_folder.get('plainName', part))
                current_folder_uuid = found_folder['uuid']
                
                if is_last_part:
                    return {
                        'type': 'folder', 'uuid': found_folder['uuid'],
                        'metadata': found_folder, 'path': '/' + '/'.join(resolved_path_parts)
                    }
                    
            elif found_file and is_last_part:
                file_name = found_file.get('plainName', '')
                file_type = found_file.get('type', '')
                full_name = f"{file_name}.{file_type}" if file_type else file_name
                resolved_path_parts.append(full_name)
                
                return {
                    'type': 'file', 'uuid': found_file['uuid'],
                    'metadata': found_file, 'path': '/' + '/'.join(resolved_path_parts)
                }
            else:
                current_path = '/' + '/'.join(resolved_path_parts + [part])
                raise FileNotFoundError(f"Path not found: {current_path}")
        
        return {
            'type': 'folder', 'uuid': current_folder_uuid,
            'metadata': {'uuid': current_folder_uuid, 'plainName': path_parts[-1] if path_parts else 'Root'},
            'path': '/' + '/'.join(resolved_path_parts)
        }

    def download_file_by_path(self, file_path: str, destination_path_str: Optional[str] = None):
        """Download file by path instead of UUID"""
        print(f"🔍 Resolving path: {file_path}")
        
        resolved = self.resolve_path(file_path)
        if resolved['type'] != 'file':
            raise ValueError(f"Path '{file_path}' is a folder, not a file")
        
        file_uuid = resolved['uuid']
        print(f"📋 Resolved to file UUID: {file_uuid}")
        
        if not destination_path_str:
            filename = Path(resolved['path']).name
            destination_path_str = f"./{filename}"
        
        return self.download_file(file_uuid, destination_path_str)

    def list_folder_with_paths(self, folder_path: str = "/") -> Dict[str, List[Dict[str, Any]]]:
        """List folder contents with full paths.

        Note: caller is responsible for any human-readable status output.
        We used to emit a `📁 Listing folder: …` line here, but that broke
        machine-readable consumers (CrispSorter's InternxtDrive parses
        `cli.py list-path --json` and choked on the leading non-JSON
        bytes).  The CLI's text-mode codepath in `cli.py:list_path`
        still echoes the header for human users.
        """
        if folder_path == "" or folder_path == "/":
            resolved = self.resolve_path("/")
        else:
            resolved = self.resolve_path(folder_path)
        
        if resolved['type'] != 'folder':
            raise ValueError(f"Path '{folder_path}' is a file, not a folder")
        
        folder_uuid = resolved['uuid']
        base_path = resolved['path']
        content = self.get_folder_content(folder_uuid)
        
        # Enhance with path info
        enhanced_folders = []
        for folder in content['folders']:
            folder_name = folder.get('plainName', folder.get('name', 'Unknown'))
            full_path = f"{base_path.rstrip('/')}/{folder_name}"
            
            enhanced_folders.append({
                **folder,
                'path': full_path,
                'display_name': folder_name,
                'size_display': '<DIR>',
                'modified': folder.get('modificationTime') or folder.get('updatedAt', ''),
            })
        
        enhanced_files = []
        for file in content['files']:
            file_name = file.get('plainName', '')
            file_type = file.get('type', '')
            display_name = f"{file_name}.{file_type}" if file_type else file_name
            full_path = f"{base_path.rstrip('/')}/{display_name}"
            
            # FIXED: Convert size string from API to integer before formatting
            try:
                size_bytes = int(file.get('size', 0))
            except (ValueError, TypeError):
                size_bytes = 0
            size_display = self._format_size(size_bytes)
            
            enhanced_files.append({
                **file,
                'path': full_path,
                'display_name': display_name,
                'size_display': size_display,
                'modified': file.get('modificationTime') or file.get('updatedAt', ''),
            })
        
        return {
            'folders': enhanced_folders,
            'files': enhanced_files,
            'current_path': base_path
        }

    def find_files(self, search_term: str, folder_path: str = "/", case_sensitive: bool = False, max_depth: int = -1) -> List[Dict[str, Any]]:
        """
        Search for files by name with wildcards, with optional case sensitivity and max depth.
        max_depth = -1 means infinite depth.
        max_depth = 1 means search *only* this folder, not subfolders.
        """
        if case_sensitive:
            print(f"🔍 Searching for '{search_term}' (case-sensitive) in {folder_path}")
        else:
            print(f"🔍 Searching for '{search_term}' (case-insensitive) in {folder_path}")
        
        results = []
        
        def search_recursive(current_path: str, current_relative_depth: int):
            # Check if we have gone too deep
            # max_depth=1 will stop recursion (depth 0 >= 1 is false)
            # max_depth=2 will allow one level (depth 1 >= 2 is false)
            if max_depth != -1 and current_relative_depth >= max_depth:
                return # Stop searching this branch
        
            try:
                # This call is cached, so it's fast
                content = self.list_folder_with_paths(current_path)
                
                # Check files in current folder
                for file in content['files']:
                    display_name = file['display_name']
                    
                    match = False
                    if case_sensitive:
                        match = fnmatch.fnmatch(display_name, search_term)
                    else:
                        match = fnmatch.fnmatch(display_name.lower(), search_term.lower())
                    
                    if match:
                        results.append({**file, 'found_in': current_path})

                # Search subfolders recursively
                for folder in content['folders']:
                    search_recursive(folder['path'], current_relative_depth + 1)
                    
            except Exception as e:
                print(f"   ⚠️  Could not search in {current_path}: {e}")
        
        search_recursive(folder_path, 0)
        
        print(f"📍 Found {len(results)} matching files")
        return results

    # ========== TRASH OPERATIONS ==========

    def trash_file(self, file_uuid: str) -> Dict[str, Any]:
        """Move file to trash"""
        try:
            self._clear_parent_cache_for_item(file_uuid, 'file')
            response = self.api.trash_file(file_uuid)  # Uses corrected bulk API
            return {'success': True, 'message': 'File moved to trash successfully', 'file': {'uuid': file_uuid}, 'result': response}
        except Exception as e:
            raise Exception(f"Failed to trash file: {e}")

    def trash_folder(self, folder_uuid: str) -> Dict[str, Any]:
        """Move folder to trash"""
        try:
            self._clear_parent_cache_for_item(folder_uuid, 'folder')
            response = self.api.trash_folder(folder_uuid)  # Uses corrected bulk API
            return {'success': True, 'message': 'Folder moved to trash successfully', 'folder': {'uuid': folder_uuid}, 'result': response}
        except Exception as e:
            raise Exception(f"Failed to trash folder: {e}")

    def trash_by_path(self, path: str) -> Dict[str, Any]:
        """Move file or folder to trash by path"""
        print(f"🗑️  Moving to trash: {path}")
        
        resolved = self.resolve_path(path)
        
        if resolved['type'] == 'file':
            return self.trash_file(resolved['uuid'])
        else:
            return self.trash_folder(resolved['uuid'])

    def delete_permanently_file(self, file_uuid: str) -> Dict[str, Any]:
        """Permanently delete file"""
        try:
            self._clear_parent_cache_for_item(file_uuid, 'file')
            self.api.delete_file(file_uuid)
            return {'success': True, 'message': 'File permanently deleted successfully'}
        except Exception as e:
            raise Exception(f"Failed to permanently delete file: {e}") from e

    def delete_permanently_folder(self, folder_uuid: str) -> Dict[str, Any]:
        """Permanently delete folder"""
        try:
            self._clear_parent_cache_for_item(folder_uuid, 'folder')
            self.api.delete_folder(folder_uuid)
            return {'success': True, 'message': 'Folder permanently deleted successfully'}
        except Exception as e:
            raise Exception(f"Failed to permanently delete folder: {e}") from e

    def delete_permanently_by_path(self, path: str) -> Dict[str, Any]:
        """Permanently delete file or folder by path"""
        print(f"🗑️  Permanently deleting: {path}")
        
        resolved = self.resolve_path(path)
        
        if resolved['type'] == 'file':
            return self.delete_permanently_file(resolved['uuid'])
        else:
            return self.delete_permanently_folder(resolved['uuid'])

    # ========== MOVE AND RENAME OPERATIONS ==========

    def move_file(self, file_uuid: str, destination_folder_uuid: str) -> Dict[str, Any]:
        """Move file to different folder"""
        try:
            self._clear_parent_cache_for_item(file_uuid, 'file')
            response = self.api.move_file(file_uuid, destination_folder_uuid)
            with self.cache_lock:
                self.folder_content_cache.pop(destination_folder_uuid, None)
            return {'success': True, 'message': f'File moved successfully to: {destination_folder_uuid}', 'result': response}
        except Exception as e:
            raise Exception(f"Failed to move file: {e}")

    def move_folder(self, folder_uuid: str, destination_folder_uuid: str) -> Dict[str, Any]:
        """Move folder to different folder"""
        try:
            self._clear_parent_cache_for_item(folder_uuid, 'folder')
            response = self.api.move_folder(folder_uuid, destination_folder_uuid)
            with self.cache_lock:
                self.folder_content_cache.pop(destination_folder_uuid, None)
            return {'success': True, 'message': f'Folder moved successfully to: {destination_folder_uuid}', 'result': response}
        except Exception as e:
            raise Exception(f"Failed to move folder: {e}")

    def rename_file(self, file_uuid: str, new_name: str) -> Dict[str, Any]:
        """Rename file"""
        try:
            # Parse name and extension
            if '.' in new_name:
                name_parts = new_name.rsplit('.', 1)
                plain_name = name_parts[0]
                file_type = name_parts[1]
            else:
                plain_name = new_name
                file_type = None
                
            response = self.api.rename_file(file_uuid, plain_name, file_type)

            self._clear_parent_cache_for_item(file_uuid, 'file')

            return {'success': True, 'message': f'File renamed successfully to: {new_name}', 'result': response}
        except Exception as e:
            raise Exception(f"Failed to rename file: {e}")

    def rename_folder(self, folder_uuid: str, new_name: str) -> Dict[str, Any]:
        """Rename folder"""
        try:
            response = self.api.rename_folder(folder_uuid, new_name)
            self._clear_parent_cache_for_item(folder_uuid, 'folder')
            return {'success': True, 'message': f'Folder renamed successfully to: {new_name}', 'result': response}
        except Exception as e:
            raise Exception(f"Failed to rename folder: {e}")
        
    def move_item(self, item_uuid: str, destination_folder_uuid: str) -> Dict[str, Any]:
        """Move file or folder to different folder (WebDAV required)"""
        try:
            # Try as file first
            try:
                return self.move_file(item_uuid, destination_folder_uuid)
            except Exception:
                # If file move fails, try as folder
                return self.move_folder(item_uuid, destination_folder_uuid)
        except Exception as e:
            raise Exception(f"Failed to move item {item_uuid}: {e}") from e

    def rename_item(self, item_uuid: str, new_name: str) -> Dict[str, Any]:
        """Rename file or folder (WebDAV required)"""
        try:
            # Try as file first
            try:
                return self.rename_file(item_uuid, new_name)
            except Exception:
                # If file rename fails, try as folder
                return self.rename_folder(item_uuid, new_name)
        except Exception as e:
            raise Exception(f"Failed to rename item {item_uuid}: {e}") from e

    def trash_item(self, item_uuid: str) -> Dict[str, Any]:
        """Move file or folder to trash (WebDAV required)"""
        try:
            # Use the corrected API trash methods
            try:
                return self.api.trash_file(item_uuid)
            except Exception:
                return self.api.trash_folder(item_uuid)
        except Exception as e:
            raise Exception(f"Failed to trash item {item_uuid}: {e}") from e
        
    def upload_with_safety_pattern(self, local_path: Path, remote_folder_uuid: str, filename: str):
        """
        Safe Upload Flow:
        1. Rename existing file to .backup
        2. Upload new file
        3. If success: delete .backup
        4. If fail: rename .backup back to original
        """
        # Check if file exists
        full_path = f"/{filename}" # Simplified for example
        existing_item = None
        try:
            existing_item = self.resolve_path(full_path)
        except FileNotFoundError:
            pass

        backup_uuid = None
        orig_name = filename
        
        if existing_item and existing_item['type'] == 'file':
            backup_name = f"{filename}.bak-{uuid.uuid4().hex[:6]}"
            print(f"⚠️ DEBUG: File conflict. Creating safety backup: {backup_name}")
            self.api.rename_file(existing_item['uuid'], backup_name)
            backup_uuid = existing_item['uuid']

        try:
            # Perform actual upload
            print(f"📤 DEBUG: Uploading {filename} to {remote_folder_uuid}...")
            new_file = self.upload_file_to_folder(str(local_path), remote_folder_uuid)
            
            # Success: Cleanup backup
            if backup_uuid:
                print(f"🗑️ DEBUG: Upload successful. Purging backup {backup_uuid}")
                self.api.delete_permanently(backup_uuid, "file")
            return new_file

        except Exception as e:
            # Failure: Rollback
            if backup_uuid:
                print(f"🚨 DEBUG: Upload FAILED. Rolling back backup to {orig_name}")
                self.api.rename_file(backup_uuid, orig_name)
            raise e

    def update_file(self, file_uuid: str, local_path: str) -> Dict[str, Any]:
        """Update existing file with new content (WebDAV required for PUT operations)"""
        try:
            # Get current file metadata
            current_metadata = self.api.get_file_metadata(file_uuid)
            plain_name = current_metadata.get('plainName', '')

            # Upload new content and get new file ID
            file_path = Path(local_path)
            file_size = file_path.stat().st_size
            
            # Get credentials and upload new version
            credentials = self.auth.get_auth_details()
            user = credentials['user']
            bucket_id = user['bucket']
            mnemonic = user['mnemonic']
            network_auth = self._get_network_auth(user)
            
            # Stream-encrypt and upload (multipart for >= 100 MiB), bounding
            # RAM by the part size and using the correct ripemd160(sha256) hash.
            timeout_seconds = max(300, int(file_size / (100 * 1024)) + 60)
            network_file_id = self._perform_network_upload(
                file_path, file_size, bucket_id, mnemonic, network_auth, timeout_seconds
            )

            # Replace file content using corrected API
            replace_payload = {
                'fileId': network_file_id,
                'size': file_size
            }
            result = self.api.replace_file(file_uuid, replace_payload)
            self._clear_parent_cache_for_item(file_uuid, 'file')
            
            return {
                'success': True,
                'message': f'File {plain_name} updated successfully',
                'result': result
            }
            
        except Exception as e:
            raise Exception(f"Failed to update file {file_uuid}: {e}")
        
    def check_file_exists(self, path: str) -> Optional[Dict[str, Any]]:
        """
        Check if a file exists at the given path without throwing an exception.
        Returns file info if exists, None otherwise.
        """
        try:
            resolved = self.resolve_path(path)
            return resolved if resolved['type'] == 'file' else None
        except FileNotFoundError:
            return None
        except Exception:
            return None

    def check_folder_exists(self, path: str) -> Optional[Dict[str, Any]]:
        """
        Check if a folder exists at the given path without throwing an exception.
        Returns folder info if exists, None otherwise.
        """
        try:
            resolved = self.resolve_path(path)
            return resolved if resolved['type'] == 'folder' else None
        except FileNotFoundError:
            return None
        except Exception:
            return None
        
    def move_by_path(self, source_path: str, target_folder_path: str) -> Dict[str, Any]:
        """
        Robustly moves an item from one path to another.
        High debug verbosity for tracing resolution errors.
        """
        print(f"🚚 TRACE: Attempting to move '{source_path}' to '{target_folder_path}'")
        
        # 1. Resolve the Source (Can be file or folder)
        source = self.resolve_path(source_path)
        source_uuid = source['uuid']
        source_type = source['type']
        print(f"🔍 TRACE: Source resolved. Type: {source_type.upper()}, UUID: {source_uuid}")

        # 2. Resolve the Target Folder
        target = self.resolve_path(target_folder_path)
        if target['type'] != 'folder':
            raise ValueError(f"Target '{target_folder_path}' is a file. You can only move items into folders.")
        
        target_uuid = target['uuid']
        print(f"🎯 TRACE: Target folder resolved. UUID: {target_uuid}")

        # 3. Perform the move based on type
        try:
            if source_type == 'file':
                result = self.api.move_file(source_uuid, target_uuid)
            else:
                result = self.api.move_folder(source_uuid, target_uuid)
            
            # 4. Cache Management: Clear parent caches so the UI updates
            with self.cache_lock:
                self.folder_content_cache.pop(target_uuid, None)
                print(f"🧹 TRACE: Cleared cache for target folder: {target_uuid}")
            
            print("✅ TRACE: Move successful!")
            return result

        except Exception as e:
            print(f"❌ TRACE: Move failed: {str(e)}")
            raise

    def copy_item(self, item_uuid: str, destination_folder_uuid: str) -> Dict[str, Any]:
        """Copy file to different folder preserving timestamps"""
        try:
            # Get file metadata
            metadata = self.api.get_file_metadata(item_uuid)
            
            # Extract timestamps - try both field name variations
            creation_time = metadata.get('creationTime') or metadata.get('createdAt')
            modification_time = metadata.get('modificationTime') or metadata.get('updatedAt')
            
            # Download file to temporary location
            import tempfile
            with tempfile.NamedTemporaryFile(delete=False) as temp_file:
                temp_path = temp_file.name
            
            try:
                self.download_file(item_uuid, temp_path)
                
                # Upload to new location WITH timestamps
                plain_name = metadata.get('plainName', '')
                file_type = metadata.get('type', '')
                
                print("     📋 Copying with timestamp preservation:")
                if creation_time:
                    print(f"        Original creation: {creation_time}")
                if modification_time:
                    print(f"        Original modification: {modification_time}")
                
                # Create new file with upload_file_to_folder, passing timestamps
                result = self.upload_file_to_folder(
                    temp_path, 
                    destination_folder_uuid, 
                    plain_name, 
                    file_type,
                    creation_time=creation_time,
                    modification_time=modification_time
                )
                
                return {
                    'success': True,
                    'message': f'File {plain_name} copied successfully',
                    'result': result,
                    'timestamps_preserved': bool(creation_time or modification_time)
                }
                
            finally:
                # Clean up temp file
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

        except Exception as e:
            raise Exception(f"Copy failed: {e}") from e
        
    def copy_folder(self, source_folder_uuid: str, destination_parent_uuid: str,
                    folder_name: Optional[str] = None) -> Dict[str, Any]:
        """Recursively copy a folder and its contents to a new parent.

        Creates the folder structure first, then copies each file via
        download-and-re-upload (same strategy as copy_item for files).
        """
        # Get source folder metadata
        source_meta = self.api.get_folder_metadata(source_folder_uuid)
        name = folder_name or source_meta.get('plainName', 'Untitled')
        creation_time = source_meta.get('creationTime') or source_meta.get('createdAt')
        modification_time = source_meta.get('modificationTime') or source_meta.get('updatedAt')

        # Create destination folder
        new_folder = self.create_folder(
            name,
            destination_parent_uuid,
            creation_time=creation_time,
            modification_time=modification_time,
        )
        new_folder_uuid = new_folder['uuid']
        print(f"     📁 Created folder: {name} → {new_folder_uuid}")

        # Get source contents
        content = self.get_folder_content(source_folder_uuid)
        files = content.get('files', [])
        folders = content.get('folders', [])

        copied_files = 0
        copied_folders = 0

        # Copy subfolders recursively
        for subfolder in folders:
            sub_uuid = subfolder.get('uuid')
            sub_name = subfolder.get('plainName', 'Untitled')
            if sub_uuid:
                self.copy_folder(sub_uuid, new_folder_uuid, sub_name)
                copied_folders += 1

        # Copy files
        for file_item in files:
            file_uuid = file_item.get('uuid')
            if file_uuid:
                self.copy_item(file_uuid, new_folder_uuid)
                copied_files += 1

        return {
            'success': True,
            'message': f'Folder "{name}" copied ({copied_files} files, {copied_folders} subfolders)',
            'uuid': new_folder_uuid,
            'files_copied': copied_files,
            'folders_copied': copied_folders,
        }

    def sanitize_filename(self, filename: str) -> str:
        """
        Sanitize filename for upload, removing or replacing problematic characters.
        """
        # Remove or replace characters that might cause issues
        invalid_chars = '<>:"/\\|?*'
        for char in invalid_chars:
            filename = filename.replace(char, '_')
        
        # Remove leading/trailing spaces and dots
        filename = filename.strip(' .')
        
        # Ensure filename is not empty
        if not filename:
            filename = 'unnamed_file'
        
        return filename

    def _find_file_entry_in_folder(self, destination_folder_uuid: str,
                                   payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Best-effort check for an entry created before an ambiguous 502."""
        with self.cache_lock:
            self.folder_content_cache.pop(destination_folder_uuid, None)
        content = self.get_folder_content(destination_folder_uuid)
        target_name = payload.get('plainName') or ''
        target_type = payload.get('type') or ''
        try:
            target_size = int(payload.get('size', 0))
        except (TypeError, ValueError):
            target_size = 0
        for item in content.get('files', []):
            try:
                item_size = int(item.get('size', 0))
            except (TypeError, ValueError):
                item_size = -1
            if (
                (item.get('plainName') or '') == target_name
                and (item.get('type') or '') == target_type
                and item_size == target_size
            ):
                return item
        return None

    def _create_file_entry_with_retry(self, payload: Dict[str, Any],
                                      destination_folder_uuid: str,
                                      max_retries: int = 4) -> Dict[str, Any]:
        last_err: Optional[BaseException] = None
        for attempt in range(max_retries):
            try:
                return self.api.create_file_entry(payload)
            except Exception as e:
                if not self._is_transient_api_error(e):
                    raise
                last_err = e
                if attempt < max_retries - 1:
                    wait = 2 ** attempt
                    print(f"     ⚠️  files metadata create failed "
                          f"(attempt {attempt + 1}/{max_retries}): {e}; "
                          f"checking destination and retrying in {wait}s...")
                    time.sleep(wait)
                    existing = self._find_file_entry_in_folder(
                        destination_folder_uuid, payload)
                    if existing is not None:
                        print("     ✅ File entry already exists after transient error")
                        return existing
        raise Exception(
            f"files metadata create failed after {max_retries} attempts: {last_err}")

    def upload_file_to_folder(self, file_path_str: str, destination_folder_uuid: str,
                            custom_name: Optional[str] = None, custom_extension: Optional[str] = None,
                            creation_time: Optional[str] = None, modification_time: Optional[str] = None):
        """Upload file with custom name/extension and optional timestamps to specific folder"""
        credentials = self.auth.get_auth_details()
        user = credentials['user']
        bucket_id = user['bucket']
        mnemonic = user['mnemonic']
        network_auth = self._get_network_auth(user)

        file_path = Path(file_path_str)
        if not file_path.is_file():
            raise FileNotFoundError(f"File not found at: {file_path}")

        file_size = file_path.stat().st_size
        if file_size > self.TWENTY_GIGABYTES:
            raise ValueError(f"File is too large (must be less than {self._format_size(self.TWENTY_GIGABYTES)})")
        
        # Use custom name/extension if provided
        file_name = custom_name or file_path.stem
        file_extension = custom_extension if custom_extension is not None else file_path.suffix.lstrip('.')
        
        display_name = f"{file_name}.{file_extension}" if file_extension else file_name
        print(f"     📤 Uploading '{display_name}' ({self._format_size(file_size)})...")
        
        # Calculate dynamic timeout based on file size (assume 100 KB/s minimum speed)
        min_speed_kbps = 100  # 100 KB/s
        timeout_seconds = max(300, int(file_size / (min_speed_kbps * 1024)) + 60)
        print(f"     ⏱️  Upload timeout: {timeout_seconds}s (~{timeout_seconds//60} minutes)")
        
        # Warn for very large files
        if file_size > 500 * 1024 * 1024:  # > 500MB
            print("     ⚠️  Large file detected - encryption may take several minutes")
            print("     💡 Please be patient, progress will be shown...")
        
        # Log timestamp preservation attempt
        if creation_time or modification_time:
            print("     🕐 Attempting to preserve timestamps:")
            if creation_time:
                print(f"        Creation: {creation_time}")
            if modification_time:
                print(f"        Modification: {modification_time}")
        
        # Streaming encrypt+upload: RAM is bounded by the part size (~30 MB),
        # not the file size, so even multi-GB files no longer OOM. Reserve a
        # small headroom so concurrent workers don't oversubscribe memory.
        #
        # Multipart (>= 100 MiB) files gate their parts INDIVIDUALLY inside
        # _perform_network_upload (the parallel per-part path). The gate is
        # process-wide and non-reentrant, so we must NOT also hold an outer
        # reservation here for those files — it would deadlock the per-part
        # acquires against this one. Single-PUT (small) files have no internal
        # gate, so they reserve here.
        use_internal_gate = file_size >= self.MULTIPART_MIN_SIZE
        mem_need = self.UPLOAD_PART_SIZE * 2
        if not use_internal_gate:
            self._mem_acquire(mem_need)
        start_total = time.time()
        try:
            network_file_id = self._perform_network_upload(
                file_path, file_size, bucket_id, mnemonic, network_auth, timeout_seconds
            )
        finally:
            if not use_internal_gate:
                self._mem_release(mem_need)

        if self.verbose:
            print(f"     [timing] network upload total: {time.time() - start_total:.3f}s")
        print(f"     ✅ Network upload complete (file id: {network_file_id})")

        # Create the Drive file entry pointing at the uploaded network file.
        file_entry_payload = {
            'folderUuid': destination_folder_uuid,
            'plainName': file_name,
            'type': file_extension if file_extension else '',
            'size': file_size,
            'bucket': bucket_id,
            'fileId': network_file_id,
            'encryptVersion': 'Aes03',
            'name': ''
        }
        if creation_time:
            file_entry_payload['creationTime'] = creation_time
        if modification_time:
            file_entry_payload['modificationTime'] = modification_time

        metadata_started = time.perf_counter()
        created_file = self._create_file_entry_with_retry(
            file_entry_payload, destination_folder_uuid)
        if self.verbose:
            print(f"     [timing] files metadata create: {time.perf_counter() - metadata_started:.3f}s")
        print(f"     ✅ File entry created (UUID: {created_file.get('uuid', 'N/A')})")

        with self.cache_lock:
            cached_item = self.folder_content_cache.get(destination_folder_uuid)
            if cached_item:
                cache_time, content = cached_item
                content['files'].append(created_file)
                self.folder_content_cache[destination_folder_uuid] = (cache_time, content)

        if creation_time or modification_time:
            returned_creation = created_file.get('creationTime') or created_file.get('createdAt')
            returned_modification = created_file.get('modificationTime') or created_file.get('updatedAt')

            if creation_time and returned_creation:
                print(f"     ✅ Creation timestamp preserved: {returned_creation}")
            elif creation_time:
                print(f"     ⚠️  Creation timestamp NOT set (API returned: {returned_creation})")

            if modification_time and returned_modification:
                print(f"     ✅ Modification timestamp preserved: {returned_modification}")
            elif modification_time:
                print(f"     ⚠️  Modification timestamp NOT set (API returned: {returned_modification})")

        total_time = time.time() - start_total
        print("\n     🎉 Upload complete!")
        print(f"        Total time: {total_time:.1f}s ({total_time/60:.1f} minutes)")
        if total_time > 0:
            print(f"        Average speed: {self._format_size(int(file_size/total_time))}/s")

        return created_file

    def _upload_chunk_with_progress(self, upload_url: str, chunk_data: bytes, timeout_seconds: int):
        """Upload chunk with custom timeout and progress tracking"""
        import requests
        
        chunk_size_mb = len(chunk_data) / (1024 * 1024)
        print(f"        Starting upload of {chunk_size_mb:.1f} MB...")
        
        # Create a custom session with longer timeout
        session = requests.Session()
        
        # For large uploads, show progress
        if len(chunk_data) > 10 * 1024 * 1024:  # > 10MB
            with tqdm(
                total=len(chunk_data),
                unit='B',
                unit_scale=True,
                desc='        Progress',
                bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt} [{rate_fmt}] ETA: {remaining}',
                leave=False
            ) as pbar:
                
                # Split into smaller chunks for progress tracking
                upload_chunk_size = 1024 * 1024  # 1MB chunks for progress
                uploaded = 0
                
                # Use streaming upload
                def data_generator():
                    nonlocal uploaded
                    while uploaded < len(chunk_data):
                        chunk = chunk_data[uploaded:uploaded + upload_chunk_size]
                        uploaded += len(chunk)
                        pbar.update(len(chunk))
                        yield chunk
                
                response = session.put(
                    upload_url,
                    data=data_generator(),
                    headers={'Content-Type': 'application/octet-stream'},
                    timeout=timeout_seconds
                )
                response.raise_for_status()
        else:
            # Small file - direct upload
            response = session.put(
                upload_url,
                data=chunk_data,
                headers={'Content-Type': 'application/octet-stream'},
                timeout=timeout_seconds
            )
            response.raise_for_status()
        
        print(f"        Upload request completed (status: {response.status_code})")

    # ========== CORE OPERATIONS ==========

    def get_folder_content(self, folder_uuid: str) -> Dict[str, List[Dict[str, Any]]]:
        """Get folder contents, with caching"""
        
        # --- BEGIN CACHE CHECK ---
        with self.cache_lock:
            cached_item = self.folder_content_cache.get(folder_uuid)
            if cached_item:
                cache_time, content = cached_item
                if (time.time() - cache_time) < self.CACHE_DURATION_SECONDS:
                    # Return the cached content if it's not expired
                    return content
        # --- END CACHE CHECK ---

        try:
            self.auth.get_auth_details()  # ensures session is initialized
            folders = self._get_all_folders(folder_uuid)
            files = self._get_all_files(folder_uuid)
            content = {'folders': folders, 'files': files}

            # --- BEGIN CACHE SET ---
            with self.cache_lock:
                # Store the new content with the current time
                self.folder_content_cache[folder_uuid] = (time.time(), content)
            # --- END CACHE SET ---
            
            return content
        except Exception as e:
            print(f"Error getting folder content: {e}")
            return {'folders': [], 'files': []}
        
    def _clear_parent_cache_for_item(self, item_uuid: str, item_type: str = 'file'):
        """Helper to find an item's parent and clear its cache."""
        parent_uuid = None
        try:
            if item_type == 'file':
                metadata = self.api.get_file_metadata(item_uuid)
                parent_uuid = metadata.get('folderUuid')
            else: # 'folder'
                metadata = self.api.get_folder_metadata(item_uuid)
                parent_uuid = metadata.get('parentUuid')
            
            if parent_uuid:
                with self.cache_lock:
                    self.folder_content_cache.pop(parent_uuid, None)
                    print(f"  -> Cache cleared for parent folder: {parent_uuid}")
        except Exception as e:
            # This is not fatal, just log it
            print(f"  -> ⚠️  Could not clear parent cache for {item_uuid} (parent: {parent_uuid}): {e}")

    def list_folder(self, folder_uuid: Optional[str] = None) -> Dict[str, List[Dict[str, Any]]]:
        """List folder contents - backward compatibility"""
        credentials = self.auth.get_auth_details()

        if not folder_uuid:
            folder_uuid = credentials['user'].get('rootFolderId', '')
            if not folder_uuid:
                raise ValueError("No root folder ID found")

        return self.get_folder_content(folder_uuid)

    def _get_all_folders(self, folder_uuid: str, offset: int = 0) -> List[Dict[str, Any]]:
        """Recursively get all folders with pagination"""
        try:
            limit = 50 
            response = self._call_api_with_retry(
                lambda: self.api.get_folder_folders(folder_uuid, offset, limit),
                f'folders/content/{folder_uuid}/folders?offset={offset}')
            folders = response.get('result', response.get('folders', []))

            if len(folders) == limit: 
                folders.extend(self._get_all_folders(folder_uuid, offset + limit)) 

            return folders
        except Exception as e:
            print(f"Warning: Failed to get folders: {e}")
            return []

    def _get_all_files(self, folder_uuid: str, offset: int = 0) -> List[Dict[str, Any]]:
        """Recursively get all files with pagination"""
        try:
            limit = 50
            response = self._call_api_with_retry(
                lambda: self.api.get_folder_files(folder_uuid, offset, limit),
                f'folders/content/{folder_uuid}/files?offset={offset}')
            files = response.get('result', response.get('files', []))

            if len(files) == limit: 
                files.extend(self._get_all_files(folder_uuid, offset + limit)) 

            return files
        except Exception as e:
            print(f"Warning: Failed to get files: {e}")
            return []
        
    def get_full_path_for_item(self, item_metadata: Dict[str, Any]) -> str:
        """
        Constructs the full, human-readable path for a file or folder
        by fetching its ancestors.
        """
        
        # Get the item's own name
        item_name = item_metadata.get('plainName', 'Unknown')
        if item_metadata.get('itemType') == 'file' and item_metadata.get('type'):
            item_name = f"{item_name}.{item_metadata['type']}"
        
        # Get the parent folder's UUID
        parent_uuid = item_metadata.get('folderUuid') # for files
        if not parent_uuid:
            parent_uuid = item_metadata.get('parentUuid') # for folders
        
        if not parent_uuid:
            # Item is in the root
            return f"/{item_name}"

        try:
            # Call the new API function
            ancestors = self.api.get_folder_ancestors(parent_uuid)
            
            # The 'ancestors' endpoint returns the list from root -> parent
            path_parts = [ancestor.get('plainName') for ancestor in ancestors]
            
            # Filter out the root folder's name (which can be 'root' or null)
            # and any other empty parts
            clean_parts = [part for part in path_parts if part and part.lower() != 'root']
            
            full_path = "/" + "/".join(clean_parts)
            
            # Add the item name itself
            return f"{full_path.rstrip('/')}/{item_name}"
        except Exception as e:
            print(f"  -> ⚠️  Could not build path for {item_name}: {e}")
            return f"/?/{item_name}" # Return a 'best guess' path
        
    def search_drive(self, search_term: str) -> List[Dict[str, Any]]:
        """
        Performs a fast, server-side fuzzy search across the entire drive.
        Note: The API only returns item names, types, and UUIDs, not full paths.
        """
        print(f"🔍 Performing server-side fuzzy search for: '{search_term}'")
        try:
            # This 'search_files' function already exists in your api.py
            results = self.api.search_files(search_term, offset=0, limit=50)
            
            # The API spec shows the data is in a 'data' key
            # If not, it might be in 'results' or the root
            items = results.get('data', results.get('results', results))
            
            if not isinstance(items, list):
                print(" -> ⚠️  Search returned an unexpected format.")
                return []
                
            return items
        except Exception as e:
            print(f" -> ❌ Search failed: {e}")
            return []

    def create_folder(self, name: str, parent_folder_uuid: Optional[str] = None,
                      creation_time: Optional[str] = None,
                      modification_time: Optional[str] = None) -> Dict[str, Any]:
        """Create new folder with optional timestamps AND update cache."""
        credentials = self.auth.get_auth_details()

        if not parent_folder_uuid:
            parent_folder_uuid = credentials['user'].get('rootFolderId', '')
            if not parent_folder_uuid:
                raise ValueError("No root folder ID found")
        
        payload = {
            'plainName': name,
            'parentFolderUuid': parent_folder_uuid
        }

        if creation_time:
            payload['creationTime'] = creation_time
            print(f"     🕐 Adding folder creationTime: {creation_time}")
        if modification_time:
            payload['modificationTime'] = modification_time
            print(f"     🕐 Adding folder modificationTime: {modification_time}")

        # 1. Create the folder via the API
        new_folder_metadata = self.api.create_folder(payload)
        
        # 2. Add the new folder to the parent's cache immediately
        with self.cache_lock:
            cached_item = self.folder_content_cache.get(parent_folder_uuid)
            if cached_item:
                cache_time, content = cached_item
                # Add new folder to the 'folders' list in the cache
                content['folders'].append(new_folder_metadata)
                # Save the updated cache content
                self.folder_content_cache[parent_folder_uuid] = (cache_time, content)
                print(f"  -> Cache updated for parent: {parent_folder_uuid}")
            # If parent isn't in cache, that's fine. It will be fetched
            # (and will include the new folder) on the next call.

        return new_folder_metadata

    def create_folder_recursive(self, path: str,
                              creation_time: Optional[str] = None,
                              modification_time: Optional[str] = None) -> Dict[str, Any]:
        """
        Ensures a folder path exists, creating intermediate folders if necessary.
        Sets timestamps *only* if the folder is being created new.
        Returns the metadata of the final folder.
        """
        credentials = self.auth.get_auth_details()
        root_folder_uuid = credentials['user'].get('rootFolderId', '')

        path = path.strip().strip('/')
        if not path:
             return {'uuid': root_folder_uuid, 'plainName': 'Root'}

        parts = [part for part in path.split('/') if part] # Clean empty parts
        current_parent_uuid = root_folder_uuid
        current_path_so_far = "/"
        final_folder_info = None

        for i, part in enumerate(parts):
            is_last_part = (i == len(parts) - 1)
            found_folder = None
            
            try:
                # 1. Check if the folder already exists
                content = self.get_folder_content(current_parent_uuid)
                for folder in content['folders']:
                    if folder.get('plainName') == part or folder.get('name') == part:
                        found_folder = folder
                        break

                if found_folder:
                    # 2. FOLDER EXISTS: We cannot update its timestamp.
                    # Just use its info and move to the next part.
                    current_parent_uuid = found_folder['uuid']
                    final_folder_info = found_folder
                    
                    if is_last_part and (creation_time or modification_time):
                        print(f"  -> ℹ️  Note: Folder '{part}' already exists. Cannot update timestamps (API limitation).")
                
                else:
                    # 3. FOLDER DOES NOT EXIST: Create it new.
                    try:
                        print(f"  -> Creating new folder: {part} in {current_path_so_far}")
                        
                        # Use self.create_folder for both intermediate and final
                        # parts so the parent cache is invalidated/updated. Only
                        # the final part gets timestamps applied.
                        if is_last_part:
                            print(f"  -> 🕐 Applying timestamps to new folder: {part}")
                            new_folder = self.create_folder(
                                part,
                                current_parent_uuid,
                                creation_time=creation_time,
                                modification_time=modification_time,
                            )
                        else:
                            new_folder = self.create_folder(part, current_parent_uuid)

                        current_parent_uuid = new_folder['uuid']
                        final_folder_info = new_folder
                    
                    except Exception as e:
                        # Handle the race condition we saw before
                        if "already exists" in str(e):
                            print(f"  -> ℹ️  Folder '{part}' already exists (consistency). Resolving...")
                            try:
                                existing_folder_path = f"{current_path_so_far.rstrip('/')}/{part}"
                                resolved = self.resolve_path(existing_folder_path)
                                current_parent_uuid = resolved['uuid']
                                final_folder_info = resolved['metadata']
                            except Exception as e2:
                                raise Exception(f"Failed to create folder '{part}' and could not resolve it after: {e2}")
                        else:
                            raise e # Re-raise other errors

                current_path_so_far = f"{current_path_so_far.rstrip('/')}/{part}"
                
                # If this is the last part, return the info we found or created
                if is_last_part:
                    return final_folder_info

            except Exception as e:
                 raise Exception(f"Failed to resolve or create folder part '{part}' in '{current_path_so_far}': {e}")

        return {'uuid': root_folder_uuid, 'plainName': 'Root'}
    
    def validate_upload_sources(self, sources: List[str], recursive: bool = False) -> Tuple[List[Path], List[str]]:
        """
        Validate upload sources and return valid files and error messages.
        
        Returns: (valid_paths, error_messages)
        """
        valid_paths = []
        errors = []
        
        for source in sources:
            source_path = Path(source)
            
            # Check if source exists
            if not source_path.exists():
                errors.append(f"Source not found: {source}")
                continue
            
            # Check if readable
            if not os.access(source_path, os.R_OK):
                errors.append(f"Source not readable: {source}")
                continue
            
            # Check file size if it's a file
            if source_path.is_file():
                try:
                    size = source_path.stat().st_size
                    if size > self.TWENTY_GIGABYTES:
                        errors.append(f"File too large (>{self._format_size(self.TWENTY_GIGABYTES)}): {source}")
                        continue
                except Exception as e:
                    errors.append(f"Cannot read file {source}: {e}")
                    continue
            
            # Check if directory but recursive not enabled
            if source_path.is_dir() and not recursive:
                errors.append(f"Directory requires --recursive flag: {source}")
                continue
            
            valid_paths.append(source_path)
        
        return valid_paths, errors
    
    def get_upload_statistics(self, local_path: Path, recursive: bool = False) -> Dict[str, Any]:
        """
        Calculate statistics for an upload operation before starting.
        Useful for showing progress and estimating time.
        
        Returns: {
            'total_files': int,
            'total_size': int,
            'total_dirs': int,
            'file_list': List[Path]
        }
        """
        stats: Dict[str, Any] = {
            'total_files': 0,
            'total_size': 0,
            'total_dirs': 0,
            'file_list': []
        }
        
        if local_path.is_file():
            stats['total_files'] = 1
            stats['total_size'] = local_path.stat().st_size
            stats['file_list'] = [local_path]
        elif local_path.is_dir():
            if recursive:
                for item in local_path.rglob('*'):
                    if item.is_file():
                        try:
                            stats['total_files'] += 1
                            stats['total_size'] += item.stat().st_size
                            stats['file_list'].append(item)
                        except Exception:
                            pass  # Skip files we can't read
                    elif item.is_dir():
                        stats['total_dirs'] += 1
            else:
                # Just count direct children
                for item in local_path.iterdir():
                    if item.is_file():
                        try:
                            stats['total_files'] += 1
                            stats['total_size'] += item.stat().st_size
                            stats['file_list'].append(item)
                        except Exception:
                            pass
                    elif item.is_dir():
                        stats['total_dirs'] += 1
        
        return stats
    
    def should_include_file(self, file_path: Path, include_patterns: List[str],
                            exclude_patterns: List[str]) -> bool:
        """Check if a file should be included based on include/exclude patterns"""
        file_name = file_path.name
        
        # If include patterns specified, file must match at least one
        if include_patterns:
            matches_include = any(fnmatch.fnmatch(file_name, pattern) for pattern in include_patterns)
            if not matches_include:
                return False
        
        # If exclude patterns specified, file must not match any
        if exclude_patterns:
            matches_exclude = any(fnmatch.fnmatch(file_name, pattern) for pattern in exclude_patterns)
            if matches_exclude:
                return False
        
        return True

    def upload_single_item_with_conflict_handling(
            self,
            local_path: Path,
            target_remote_parent_path_str: str,
            target_folder_uuid: str,
            on_conflict: str,
            remote_filename: Optional[str] = None,
            creation_time: Optional[str] = None,
            modification_time: Optional[str] = None
        ) -> str:
        """
        Uploads a single local file, handling conflicts based on the specified strategy.
        
        Args:
            local_path: Path object for the local file.
            target_remote_parent_path_str: The full intended remote path of the PARENT folder.
            target_folder_uuid: The UUID of the *immediate parent* remote folder to upload into.
            on_conflict: 'skip' or 'overwrite'.
            remote_filename: If specified, use this filename instead of local_path.name.
            creation_time: ISO format timestamp for file creation (optional).
            modification_time: ISO format timestamp for file modification (optional).

        Returns:
            "uploaded", "skipped", or "error"
        """
        if not local_path.is_file():
            print(f"  -> ⚠️  Not a file, skipping: {local_path}")
            return "skipped"

        # Validate file size before proceeding
        try:
            file_size = local_path.stat().st_size
            if file_size > self.TWENTY_GIGABYTES:
                print(f"  -> ❌ File too large (>{self._format_size(self.TWENTY_GIGABYTES)}): {local_path.name}")
                return "error"
            if file_size == 0:
                print(f"  -> ⚠️  File is empty, skipping: {local_path.name}")
                return "skipped"
        except Exception as e:
            print(f"  -> ❌ Cannot read file: {e}")
            return "error"

        effective_remote_filename = remote_filename or local_path.name
        
        # Construct the full path of the potential target FILE for existence check
        full_target_remote_path = f"{target_remote_parent_path_str.rstrip('/')}/{effective_remote_filename}"
        if full_target_remote_path.startswith('//'):
            full_target_remote_path = full_target_remote_path[1:]
        if not full_target_remote_path.startswith('/'):
            full_target_remote_path = '/' + full_target_remote_path

        print(f"  -> Preparing upload: '{local_path.name}' ({self._format_size(file_size)}) to '{full_target_remote_path}'")
        
        if creation_time or modification_time:
            print("  -> 🕐 With timestamp preservation")

        file_stem = Path(effective_remote_filename).stem
        file_suffix = Path(effective_remote_filename).suffix.lstrip('.')

        if not file_suffix and '.' not in effective_remote_filename:
            file_suffix = ''
        elif not file_suffix and '.' in effective_remote_filename:
            file_stem = effective_remote_filename
            file_suffix = ''

        existing_item_info = None
        try:
            existing_item_info = self.resolve_path(full_target_remote_path)
            print(f"  -> Target exists: {full_target_remote_path} (Type: {existing_item_info['type']})")
        except FileNotFoundError:
            print("  -> Target does not exist, proceeding with upload")
            pass
        except Exception as e:
            print(f"  -> ⚠️  Error checking target existence: {e}")

        if existing_item_info:
            if on_conflict == 'skip':
                print("  -> ⏭️  Skipping due to conflict policy (file exists)")
                return "skipped"
            elif on_conflict == 'overwrite':
                if existing_item_info['type'] == 'folder':
                    print(f"  -> ❌ Cannot overwrite folder with a file: {full_target_remote_path}")
                    return "error"
                else:
                    print("  -> 🔄 Overwriting existing file...")
                    try:
                        self.delete_permanently_by_path(full_target_remote_path)
                        print("  -> 🗑️  Deleted existing file for overwrite")
                    except Exception as del_err:
                        print(f"  -> ❌ Error deleting existing file for overwrite: {del_err}")
                        return "error"
            else:
                print(f"  -> ❌ Invalid conflict mode '{on_conflict}'")
                return "error"
        elif on_conflict == 'skip':
            # The path cache can be stale or miss files after a large cached
            # pre-scan. Before spending bandwidth on a "missing" file, force a
            # parent listing and check the exact Drive entry by name/type/size.
            existing_file = self._find_file_entry_in_folder(
                target_folder_uuid,
                {
                    'plainName': file_stem,
                    'type': file_suffix if file_suffix else '',
                    'size': file_size,
                },
            )
            if existing_file is not None:
                print("  -> ⏭️  Skipping due to conflict policy "
                      "(file exists after parent refresh)")
                return "skipped"

        # --- Proceed with upload ---
        try:
            # Upload with timestamps
            self.upload_file_to_folder(
                str(local_path),
                target_folder_uuid,
                custom_name=file_stem,
                custom_extension=file_suffix if file_suffix else None,
                creation_time=creation_time,
                modification_time=modification_time
            )
            print(f"  -> ✅ Successfully uploaded: {effective_remote_filename}")
            return "uploaded"
        except Exception as up_err:
            if on_conflict == 'skip' and "File already exists" in str(up_err):
                print("  -> ⏭️  Skipping due to conflict policy "
                      "(server reported file already exists)")
                return "skipped"
            print(f"  -> ❌ Error during upload: {up_err}")
            import traceback
            traceback.print_exc()
            return "error"

    class _RangeNotSupported(Exception):
        """Raised when the S3 endpoint answers a ranged GET with 200 (whole
        object) instead of 206 — the caller falls back to a single stream."""

    def _download_sequential(self, download_url: str, file_size: int, mnemonic: str,
                             bucket_id: str, file_index_hex: str,
                             destination_path: Path, timeout_seconds: int) -> int:
        """Stream one presigned GET and CTR-decrypt to disk; return bytes written."""
        decryptor = self.crypto.new_download_decryptor(mnemonic, bucket_id, file_index_hex)
        DL_CHUNK = 4 * 1024 * 1024
        written = 0
        with self.api.download_stream(download_url, timeout=timeout_seconds) as resp, \
                open(destination_path, 'wb') as out, \
                tqdm(total=file_size, unit='B', unit_scale=True, desc='        Progress',
                     bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt} [{rate_fmt}] ETA: {remaining}',
                     leave=False) as dbar:
            for enc_chunk in resp.iter_content(chunk_size=DL_CHUNK):
                if not enc_chunk:
                    continue
                plain = decryptor.update(enc_chunk)
                # Defensively trim so we never write past the real file size.
                if written + len(plain) > file_size:
                    plain = plain[:file_size - written]
                if plain:
                    out.write(plain)
                    written += len(plain)
                dbar.update(len(enc_chunk))
            tail = decryptor.finalize()
            if tail and written < file_size:
                tail = tail[:file_size - written]
                out.write(tail)
                written += len(tail)
        return written

    def _download_ranged(self, download_url: str, file_size: int, mnemonic: str,
                         bucket_id: str, file_index_hex: str, destination_path: Path,
                         timeout_seconds: int, n_workers: int) -> int:
        """Fetch the file as N 16-byte-aligned ranges concurrently, CTR-decrypt
        each at its offset, and write it positionally; return bytes written.

        AES-CTR is seekable, so each range decrypts independently of the others.
        Ranges finish out of order but are written at their byte offset under a
        lock, so the result is byte-identical to the sequential path. Bytes in
        flight are bounded by both the worker pool and the memory gate. Raises
        ``_RangeNotSupported`` if the server ignores Range (200 not 206), so the
        caller can fall back to a single stream.
        """
        part_size = self.DOWNLOAD_PART_SIZE  # 30 MB, a multiple of 16
        n_parts = math.ceil(file_size / part_size)
        print(f"        Ranged download: {n_parts} range(s) of {self._format_size(part_size)}")

        # Cheap 1-byte probe: bail out to the sequential path before doing any
        # real work if the endpoint doesn't honour Range.
        probe_status, _ = self.api.download_range(download_url, 0, 0, timeout_seconds)
        if probe_status != 206:
            raise self._RangeNotSupported()

        errors: List[Tuple[int, BaseException]] = []
        errors_lock = threading.Lock()
        write_lock = threading.Lock()
        inflight = threading.BoundedSemaphore(n_workers)
        written_total = 0

        with open(destination_path, 'wb') as out, tqdm(
                total=file_size, unit='B', unit_scale=True, desc='        Progress',
                bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt} [{rate_fmt}] ETA: {remaining}',
                leave=False) as dbar:
            out.truncate(file_size)

            def _fetch_range(idx: int) -> int:
                nonlocal written_total
                start = idx * part_size
                end = min(start + part_size, file_size) - 1
                length = end - start + 1
                try:
                    status, content = self.api.download_range(
                        download_url, start, end, timeout_seconds)
                    if status != 206:
                        raise self._RangeNotSupported()
                    decryptor = self.crypto.new_download_decryptor_at(
                        mnemonic, bucket_id, file_index_hex, start)
                    plain = decryptor.update(content) + decryptor.finalize()
                    if len(plain) > length:
                        plain = plain[:length]
                    with write_lock:
                        out.seek(start)
                        out.write(plain)
                        written_total += len(plain)
                    dbar.update(length)
                    return len(plain)
                except BaseException as e:  # noqa: BLE001 — surfaced after join
                    with errors_lock:
                        errors.append((idx, e))
                    return 0
                finally:
                    self._mem_release(length)
                    inflight.release()

            executor = concurrent.futures.ThreadPoolExecutor(max_workers=n_workers)
            try:
                for idx in range(n_parts):
                    with errors_lock:
                        if errors:
                            break
                    start = idx * part_size
                    end = min(start + part_size, file_size) - 1
                    inflight.acquire()
                    self._mem_acquire(end - start + 1)
                    executor.submit(_fetch_range, idx)
            finally:
                executor.shutdown(wait=True)

        if errors:
            errors.sort(key=lambda t: t[0])
            idx, err = errors[0]
            if isinstance(err, self._RangeNotSupported):
                raise err  # trigger the sequential fallback
            raise Exception(f"Ranged download failed on range {idx + 1}/{n_parts}: {err}")
        return written_total

    def download_file(self, file_uuid: str, destination_path_str: str,
                    preserve_timestamps: bool = False):
        """Download and decrypt file with optional timestamp preservation"""
        credentials = self.auth.get_auth_details()
        user = credentials['user']
        mnemonic = user['mnemonic']
        network_auth = self._get_network_auth(user)
        
        print(f"📥 Downloading file UUID: {file_uuid} ...")
        
        with tqdm(total=5, desc="Downloading", unit="step", 
                bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}]') as pbar:
            
            pbar.set_description("📋 Fetching file metadata")
            metadata = self.api.get_file_metadata(file_uuid)
            
            bucket_id = metadata['bucket']
            network_file_id = metadata['fileId']
            file_size = int(metadata['size'])
            file_name = metadata.get('plainName', 'downloaded_file')
            file_type = metadata.get('type')
            
            # Extract timestamps from metadata
            creation_time = metadata.get('creationTime') or metadata.get('createdAt')
            modification_time = metadata.get('modificationTime') or metadata.get('updatedAt')
            
            if file_type:
                file_name = f"{file_name}.{file_type}"
            
            print(f"     📄 File: {file_name}")
            print(f"     📊 Size: {self._format_size(file_size)}")
            if preserve_timestamps:
                print("     🕐 Remote timestamps:")
                if creation_time:
                    print(f"        Creation: {creation_time}")
                if modification_time:
                    print(f"        Modification: {modification_time}")
            
            pbar.update(1)
            
            pbar.set_description("🔗 Fetching download links")
            links_response = self.api.get_download_links(bucket_id, network_file_id, auth=network_auth)
            download_url = links_response['shards'][0]['url']
            file_index_hex = links_response['index']
            print("     🔗 Download URL acquired")
            pbar.update(1)
            
            # Resolve the destination path up front so we can stream to disk.
            destination_path = Path(destination_path_str)
            if destination_path.is_dir():
                destination_path = destination_path / file_name

            # Stream-download and decrypt directly to disk so RAM stays bounded
            # regardless of file size (AES-CTR decrypts incrementally). When
            # ranged downloads are enabled and the file is large, split it into
            # N 16-byte-aligned ranges fetched concurrently (each CTR-decrypted
            # at its offset); otherwise — and on a Range-unsupported server —
            # use one sequential stream.
            pbar.set_description("☁️  Downloading + decrypting")
            timeout_seconds = max(300, int(file_size / (100 * 1024)) + 60)
            use_ranged = (self.ranged_download
                          and file_size >= self.RANGED_DOWNLOAD_MIN_SIZE)
            written = 0
            if use_ranged:
                try:
                    written = self._download_ranged(
                        download_url, file_size, mnemonic, bucket_id,
                        file_index_hex, destination_path, timeout_seconds,
                        max(1, self.chunk_workers))
                except self._RangeNotSupported:
                    print("     ↩️  Server ignored Range (HTTP 200) — "
                          "falling back to a single sequential stream")
                    written = self._download_sequential(
                        download_url, file_size, mnemonic, bucket_id,
                        file_index_hex, destination_path, timeout_seconds)
            else:
                written = self._download_sequential(
                    download_url, file_size, mnemonic, bucket_id,
                    file_index_hex, destination_path, timeout_seconds)
            print(f"     ☁️  Downloaded + decrypted {self._format_size(written)} to disk")
            print(f"     💾 Saved to: {destination_path}")
            pbar.update(3)  # collapses the old download / decrypt / save steps
        
        # Set timestamps if requested
        if preserve_timestamps and (creation_time or modification_time):
            try:
                from datetime import datetime

                destination_path.stat()
                
                # Parse timestamps
                if modification_time:
                    try:
                        # Try parsing ISO format
                        mtime = datetime.fromisoformat(modification_time.replace('Z', '+00:00'))
                        mtime_ts = mtime.timestamp()
                        
                        # Set access and modification times
                        os.utime(destination_path, (mtime_ts, mtime_ts))
                        print(f"     🕐 Set modification time: {modification_time}")
                    except Exception as e:
                        print(f"     ⚠️  Could not set modification time: {e}")
                
                # Note: Setting creation time is platform-specific and often not supported
                if creation_time:
                    print("     ℹ️  Note: Creation time cannot be set on most systems")
                    
            except Exception as e:
                print(f"     ⚠️  Could not preserve timestamps: {e}")
        
        print(f"✅ Success! File downloaded to '{destination_path}'")
        return str(destination_path)

    def _format_size(self, size_bytes: int) -> str:
        """Convert bytes to human readable format"""
        if not size_bytes:
            return "0 B"

        size: float = float(size_bytes)
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0

        return f"{size:.1f} PB"

    def get_file_metadata(self, file_uuid: str) -> Dict[str, Any]:
        """Get file metadata"""
        self.auth.get_auth_details()  # ensures session is initialized
        return self.api.get_file_metadata(file_uuid)

    def get_folder_metadata(self, folder_uuid: str) -> Dict[str, Any]:
        """Get folder metadata"""
        self.auth.get_auth_details()  # ensures session is initialized
        return self.api.get_folder_metadata(folder_uuid)

    def set_folder_timestamps(self, folder_uuid: str,
                              creation_time: Optional[str] = None,
                              modification_time: Optional[str] = None) -> Dict[str, Any]:
        """Update creation/modification timestamps on an existing folder.

        Used by the WebDAV provider's PROPPATCH handler so file managers
        (Finder, Explorer) can set folder timestamps on the remote.
        """
        if not creation_time and not modification_time:
            raise ValueError("Must provide creation_time or modification_time")
        payload: Dict[str, Any] = {}
        if creation_time:
            payload['creationTime'] = creation_time
        if modification_time:
            payload['modificationTime'] = modification_time
        result = self.api.update_folder_metadata(folder_uuid, payload)
        # Invalidate the parent-folder cache so subsequent listings see new times.
        self._clear_parent_cache_for_item(folder_uuid, 'folder')
        return result

    def set_file_timestamps(self, file_uuid: str,
                            creation_time: Optional[str] = None,
                            modification_time: Optional[str] = None) -> Dict[str, Any]:
        """Update creation/modification timestamps on an existing file.

        Used by the WebDAV provider's PROPPATCH handler so file managers
        and rclone --metadata can set file timestamps on the remote.
        """
        if not creation_time and not modification_time:
            raise ValueError("Must provide creation_time or modification_time")
        payload: Dict[str, Any] = {}
        if creation_time:
            payload['creationTime'] = creation_time
        if modification_time:
            payload['modificationTime'] = modification_time
        result = self.api.update_file_metadata(file_uuid, payload)
        self._clear_parent_cache_for_item(file_uuid, 'file')
        return result


# Global instance
drive_service = DriveService()
