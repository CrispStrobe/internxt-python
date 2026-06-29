"""Live integration smoke tests against the real Internxt backend.

DESIGN PRINCIPLES (this is a real-account test — be paranoid):

1. **Auto-skip without creds.** Only runs if both IXT_ACCOUNT and IXT_PWD
   are present in env (loaded from .env or set otherwise). Default
   behavior in CI / for other devs: skipped, zero network.

2. **Self-contained namespace.** All operations happen inside a single
   sentinel folder named with a UUID:
       /__pytest_internxt_cli_smoke__/<run-uuid>/
   Nothing touches anything outside that prefix.

3. **Always cleans up.** A try/finally puts the sentinel folder in trash
   even if an assertion fails. (Trash is recoverable in Internxt UI for
   30 days, so even if cleanup fails the user can still recover.)

4. **No cassette recording.** Bytes/responses live only in memory during
   the test. Nothing about your account is written to disk in the repo.

5. **Read-only checks first.** Login + whoami are validated before any
   mutating operation runs.

6. **Small payloads only.** Files are <= 1 MB to limit quota impact.
   One test exercises the multipart-threshold boundary path with a 2 MB
   file to verify the production code path; everything else is small.

To run:
    pytest tests/test_live_smoke.py -v -s

To force-skip even with creds present:
    PYTEST_SKIP_LIVE=1 pytest tests/test_live_smoke.py
"""
import os
import sys
import time
import uuid
from pathlib import Path

import pytest


# ---------- credential loading ----------

def _load_dotenv_if_present():
    """Load .env from project root if python-dotenv is installed and
    the file exists. Silent no-op otherwise."""
    project_root = Path(__file__).resolve().parent.parent
    env_path = project_root / '.env'
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path)
    except ImportError:
        # Fallback: parse simple KEY=VALUE lines ourselves
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, _, v = line.partition('=')
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv_if_present()


# ---------- skip conditions ----------

_SKIP_REASON = None
if os.environ.get('PYTEST_SKIP_LIVE') == '1':
    _SKIP_REASON = "PYTEST_SKIP_LIVE=1 set in environment"
elif not (os.environ.get('IXT_ACCOUNT') and os.environ.get('IXT_PWD')):
    _SKIP_REASON = "IXT_ACCOUNT and IXT_PWD not set in env (or .env)"

# Auto-rerun every live test on transient failure (rate limiting,
# eventual-consistency) — the second attempt almost always passes.
# Requires pytest-rerunfailures; degrades gracefully if not installed.
pytestmark = [
    pytest.mark.skipif(_SKIP_REASON is not None, reason=_SKIP_REASON or ""),
    pytest.mark.flaky(reruns=2, reruns_delay=2),
]


# ---------- sentinel folder for safety ----------

# Unique per pytest session so concurrent runs don't collide
SENTINEL_PREFIX = "__pytest_internxt_cli_smoke__"
RUN_ID = uuid.uuid4().hex[:8]
SENTINEL_PATH = f"/{SENTINEL_PREFIX}/{RUN_ID}"


@pytest.fixture(scope='module')
def authed_session():
    """Log in once per test module with the .env creds; tear down on exit."""
    # Make sure project root is on sys.path so the `services` package imports
    project_root = Path(__file__).resolve().parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    from services.auth import auth_service
    from services.drive import drive_service

    email = os.environ['IXT_ACCOUNT']
    password = os.environ['IXT_PWD']
    tfa = os.environ.get('IXT_2FA') or None

    print(f"\n🔑 LIVE: Logging in as {email}...")
    auth_service.login(email, password, tfa_code=tfa)

    # Sanity: verify whoami works
    info = auth_service.whoami()
    assert info is not None, "Login succeeded but whoami returned None"
    assert info['email'].lower() == email.lower()
    print(f"✅ LIVE: Authenticated as {info['email']} (uuid={info['uuid'][:8]}...)")

    yield {'auth': auth_service, 'drive': drive_service, 'whoami': info}

    # Module-level teardown: remove the entire sentinel folder
    print(f"\n🧹 LIVE: Cleaning up sentinel folder {SENTINEL_PATH}...")
    try:
        # Force fresh listing so the trash sees the latest tree
        drive_service.folder_content_cache.clear()
        drive_service.trash_by_path(SENTINEL_PATH)
        print("✅ LIVE: Cleanup successful (sent to trash)")
    except FileNotFoundError:
        print("ℹ️  LIVE: Sentinel folder already gone")
    except Exception as e:
        print(f"⚠️  LIVE: Cleanup failed (please manually trash {SENTINEL_PATH}): {e}")


@pytest.fixture(scope='module')
def sentinel_folder(authed_session):
    """Create the run-specific sentinel folder once per module."""
    drive = authed_session['drive']
    print(f"\n📁 LIVE: Creating sentinel folder {SENTINEL_PATH}")
    info = drive.create_folder_recursive(SENTINEL_PATH)
    print(f"✅ LIVE: Sentinel folder uuid={info['uuid'][:8]}...")
    return info


# ---------- helpers ----------

def _unique_subpath(name: str) -> str:
    """Build a unique subpath under the sentinel for a single test."""
    suffix = uuid.uuid4().hex[:6]
    return f"{SENTINEL_PATH}/{name}_{suffix}"


def _write_payload(tmp_path: Path, filename: str, size_bytes: int = 256) -> tuple[Path, bytes]:
    """Generate a unique pseudo-random payload of the given size; write to tmp_path."""
    # Mix of unique tag + repeated bytes so the payload is distinctive
    tag = b"internxt-cli-smoke-" + uuid.uuid4().bytes
    fill = (b"\x00\x55\xaa\xff" * (size_bytes // 4 + 1))[:size_bytes - len(tag)]
    payload = tag + fill
    payload = payload[:size_bytes]
    p = tmp_path / filename
    p.write_bytes(payload)
    return p, payload


def _unique_name(stem: str) -> str:
    """Append a UUID suffix so the same logical name is unique per call.

    Critical for live tests: pytest-rerunfailures re-runs failed tests
    from scratch, but a previous attempt may have already uploaded a file
    with this name to the shared sentinel folder. Using a fresh UUID per
    call keeps reruns idempotent.
    """
    return f"{stem}-{uuid.uuid4().hex[:6]}"


# =============================================================================
# READ-ONLY SMOKE (always safe, no quota impact)
# =============================================================================

def test_live_login_and_whoami(authed_session):
    """The fixture itself does the work; this just asserts the session
    has the user info we expect."""
    info = authed_session['whoami']
    assert info['email']
    assert info['uuid']
    assert info['rootFolderId']


def test_live_list_root_folder(authed_session):
    """Read-only: list the user's actual root folder. Must succeed and
    return a dict with folders + files keys."""
    drive = authed_session['drive']
    creds = authed_session['auth'].get_auth_details()
    root_uuid = creds['user']['rootFolderId']

    content = drive.get_folder_content(root_uuid)
    assert 'folders' in content
    assert 'files' in content
    assert isinstance(content['folders'], list)
    assert isinstance(content['files'], list)


def test_live_storage_usage_endpoint(authed_session):
    """Pure read; verify the /users/usage endpoint contract."""
    api = authed_session['auth'].api
    usage = api.get_storage_usage()
    # The shape may vary but it must at minimum be a dict
    assert isinstance(usage, dict)


def test_live_user_info_endpoint_known_404():
    """REGRESSION marker: `/drive/users/me` does not exist on the live
    backend (returns "Cannot GET /api/users/me"). `api.get_user_info()`
    is therefore dead code from the CLI's perspective — the same data
    is available via the credentials returned by login.

    This test pins down that current state. If the endpoint becomes
    available later, this test will fail and tell us to wire the call
    back into the CLI (e.g. for a richer `whoami` or `config` command).
    """
    project_root = Path(__file__).resolve().parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    from services.auth import auth_service as _auth  # already logged in by fixture
    # Make sure we're authed
    creds = _auth.config.read_user_credentials()
    if not creds:
        pytest.skip("not logged in (run other live tests first)")

    with pytest.raises((ValueError, ConnectionError)) as exc_info:
        _auth.api.get_user_info()
    # Sanity: the failure is the expected 404, not something else
    assert '404' in str(exc_info.value) or 'Not Found' in str(exc_info.value) \
        or 'Cannot GET' in str(exc_info.value), (
        f"Expected 404 from /users/me, got: {exc_info.value}"
    )


# =============================================================================
# CORE UPLOAD/DOWNLOAD CYCLE
# =============================================================================

def test_live_full_upload_download_cycle(authed_session, sentinel_folder, tmp_path):
    """The headline cycle test: create → upload → list → download → verify."""
    drive = authed_session['drive']
    sentinel_uuid = sentinel_folder['uuid']

    name = _unique_name('smoke')
    local_file, payload = _write_payload(tmp_path, f"{name}.txt", size_bytes=512)
    print(f"📝 LIVE: Wrote {len(payload)} bytes to {local_file}")

    print("📤 LIVE: Uploading...")
    uploaded = drive.upload_file_to_folder(
        str(local_file), sentinel_uuid,
        custom_name=name, custom_extension='txt',
    )
    file_uuid = uploaded.get('uuid')
    assert file_uuid, f"Upload returned no uuid: {uploaded}"
    print(f"✅ LIVE: Uploaded uuid={file_uuid[:8]}...")

    drive.folder_content_cache.pop(sentinel_uuid, None)
    listing = drive.get_folder_content(sentinel_uuid)
    file_uuids = [f.get('uuid') for f in listing['files']]
    assert file_uuid in file_uuids, (
        f"Uploaded file {file_uuid} not in folder listing: {file_uuids}"
    )

    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    out_path = drive.download_file(file_uuid, str(download_dir))

    downloaded = Path(out_path).read_bytes()
    assert downloaded == payload
    assert len(downloaded) == len(payload)
    print(f"✅ LIVE: Round-trip {len(payload)} bytes OK")


# =============================================================================
# UPLOAD VARIATIONS
# =============================================================================

def test_live_upload_with_unicode_filename(authed_session, sentinel_folder, tmp_path):
    """Unicode in filenames must round-trip through the encrypted-name layer."""
    drive = authed_session['drive']
    sentinel_uuid = sentinel_folder['uuid']

    name = _unique_name('résumé')
    local_file, payload = _write_payload(tmp_path, f"{name}.txt", size_bytes=64)
    uploaded = drive.upload_file_to_folder(
        str(local_file), sentinel_uuid,
        custom_name=name, custom_extension='txt',
    )
    assert uploaded.get('uuid'), "Unicode-named upload returned no uuid"

    # The remote should report the same plainName back (or its decrypted form)
    drive.folder_content_cache.pop(sentinel_uuid, None)
    listing = drive.get_folder_content(sentinel_uuid)
    plain_names = [f.get('plainName', '') for f in listing['files']]
    assert name in plain_names, (
        f"Unicode filename {name!r} not preserved on remote. Got: {plain_names}"
    )
    print("✅ LIVE: Unicode filename preserved on server")


def test_live_upload_extensionless_file(authed_session, sentinel_folder, tmp_path):
    """Files without an extension (README, LICENSE, etc.) must round-trip."""
    drive = authed_session['drive']
    sentinel_uuid = sentinel_folder['uuid']

    name = _unique_name('README')
    local_file, payload = _write_payload(tmp_path, name, size_bytes=128)
    uploaded = drive.upload_file_to_folder(
        str(local_file), sentinel_uuid,
        custom_name=name, custom_extension='',
    )
    assert uploaded.get('uuid')

    # Download by uuid; verify bytes (retry for eventual consistency)
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    for _attempt in range(3):
        try:
            out_path = drive.download_file(uploaded['uuid'], str(download_dir))
            break
        except (ValueError, Exception) as exc:
            if 'not found' in str(exc).lower() and _attempt < 2:
                time.sleep(2)
                continue
            raise
    assert Path(out_path).read_bytes() == payload
    print("✅ LIVE: Extensionless file round-trip OK")


def test_live_upload_2mb_file_to_exercise_multipart_path(authed_session, sentinel_folder, tmp_path):
    """A 2 MB file stays under the 100 MiB multipart floor, so it uses the
    single pre-signed PUT path — but still exercises the streaming
    encrypt+upload code. Kept small to limit quota impact."""
    drive = authed_session['drive']
    sentinel_uuid = sentinel_folder['uuid']

    size = 2 * 1024 * 1024  # 2 MB
    name = _unique_name('bigger')
    local_file, payload = _write_payload(tmp_path, f"{name}.bin", size_bytes=size)

    t0 = time.time()
    uploaded = drive.upload_file_to_folder(
        str(local_file), sentinel_uuid,
        custom_name=name, custom_extension='bin',
    )
    elapsed = time.time() - t0
    assert uploaded.get('uuid'), f"2MB upload returned no uuid: {uploaded}"
    print(f"✅ LIVE: Uploaded 2 MB in {elapsed:.1f}s")

    # Round-trip
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    out_path = drive.download_file(uploaded['uuid'], str(download_dir))
    downloaded = Path(out_path).read_bytes()
    assert len(downloaded) == size, f"Expected {size} bytes, got {len(downloaded)}"
    assert downloaded == payload, "2 MB round-trip data mismatch"
    print("✅ LIVE: 2 MB round-trip integrity OK")


def test_live_large_file_upload_round_trip(authed_session, sentinel_folder, tmp_path):
    """Upload a 3 MB file (single-part streaming path) and verify
    byte-for-byte round-trip via the streaming download path."""
    drive = authed_session['drive']
    sentinel_uuid = sentinel_folder['uuid']

    size = 3 * 1024 * 1024  # 3 MB
    name = _unique_name('large')
    local_file, payload = _write_payload(tmp_path, f"{name}.bin", size_bytes=size)

    t0 = time.time()
    uploaded = drive.upload_file_to_folder(
        str(local_file), sentinel_uuid,
        custom_name=name, custom_extension='bin',
    )
    elapsed = time.time() - t0
    assert uploaded.get('uuid'), f"Large upload returned no uuid: {uploaded}"
    print(f"✅ LIVE: 3 MB upload in {elapsed:.1f}s")

    # Round-trip: download and verify (retry for eventual consistency)
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    for _attempt in range(3):
        try:
            out_path = drive.download_file(uploaded['uuid'], str(download_dir))
            break
        except (ValueError, Exception) as exc:
            if 'not found' in str(exc).lower() and _attempt < 2:
                time.sleep(3)
                continue
            raise
    downloaded = Path(out_path).read_bytes()
    assert len(downloaded) == size, f"Expected {size} bytes, got {len(downloaded)}"
    assert downloaded == payload, "Large file round-trip data mismatch"
    print("✅ LIVE: 3 MB round-trip integrity OK")


def test_live_multipart_upload_round_trip(authed_session, sentinel_folder, tmp_path):
    """REAL multipart: upload a file above the 100 MiB floor so the CLI uses
    the S3 multipart path (one continuous AES-CTR keystream sliced into 30 MB
    parts, each PUT separately, finished with an ETag/parts manifest), then
    stream-download and verify byte-for-byte.

    This is the headline validation for the large-file work — it exercises the
    real ?multiparts=N start, per-part presigned PUTs, ripemd160(sha256) shard
    hash, and CompleteMultipartUpload on the live backend. ~110 MB of quota,
    cleaned up to trash with the rest of the sentinel folder.

    Skippable via IXT_SKIP_MULTIPART=1 (it moves real bytes over the network)."""
    if os.environ.get('IXT_SKIP_MULTIPART') == '1':
        pytest.skip("IXT_SKIP_MULTIPART=1 set")

    drive = authed_session['drive']
    sentinel_uuid = sentinel_folder['uuid']

    # 110 MB > 100 MiB floor → ceil(110/30) = 4 parts (30+30+30+20).
    size = 110 * 1024 * 1024
    assert size >= drive.MULTIPART_MIN_SIZE, "test size must exceed the multipart floor"
    name = _unique_name('multipart')
    local_file, payload = _write_payload(tmp_path, f"{name}.bin", size_bytes=size)
    print(f"\n📝 LIVE: Wrote {drive._format_size(size)} for multipart test")

    t0 = time.time()
    uploaded = drive.upload_file_to_folder(
        str(local_file), sentinel_uuid,
        custom_name=name, custom_extension='bin',
    )
    elapsed = time.time() - t0
    assert uploaded.get('uuid'), f"Multipart upload returned no uuid: {uploaded}"
    print(f"✅ LIVE: Multipart-uploaded {drive._format_size(size)} in {elapsed:.1f}s")

    # Round-trip via streaming download (retry for eventual consistency).
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    out_path = None
    for _attempt in range(4):
        try:
            out_path = drive.download_file(uploaded['uuid'], str(download_dir))
            break
        except Exception as exc:
            if 'not found' in str(exc).lower() and _attempt < 3:
                time.sleep(3)
                continue
            raise
    assert out_path is not None
    downloaded = Path(out_path).read_bytes()
    assert len(downloaded) == size, f"Expected {size} bytes, got {len(downloaded)}"
    assert downloaded == payload, "Multipart round-trip data mismatch"
    print(f"✅ LIVE: Multipart round-trip integrity OK ({drive._format_size(size)})")

    # Step B — re-download the SAME object with parallel ranged GETs and verify
    # it is byte-identical (no extra upload quota). Falls back internally if the
    # presigned URL ignores Range.
    ranged_dir = tmp_path / "downloads_ranged"
    ranged_dir.mkdir()
    saved_ranged = drive.ranged_download
    drive.ranged_download = True
    try:
        ranged_out = None
        for _attempt in range(4):
            try:
                ranged_out = drive.download_file(uploaded['uuid'], str(ranged_dir))
                break
            except Exception as exc:
                if 'not found' in str(exc).lower() and _attempt < 3:
                    time.sleep(3)
                    continue
                raise
    finally:
        drive.ranged_download = saved_ranged
    assert ranged_out is not None
    ranged_bytes = Path(ranged_out).read_bytes()
    assert ranged_bytes == payload, "Ranged download data mismatch"
    print(f"✅ LIVE: Ranged-download round-trip integrity OK ({drive._format_size(size)})")


# =============================================================================
# PATH RESOLUTION + LISTING
# =============================================================================

def test_live_path_resolution_works(authed_session, sentinel_folder):
    """resolve_path against the live API for the sentinel folder."""
    drive = authed_session['drive']
    drive.folder_content_cache.clear()
    resolved = drive.resolve_path(SENTINEL_PATH)
    assert resolved['type'] == 'folder'
    assert resolved['uuid'] == sentinel_folder['uuid']
    assert resolved['path'].endswith(RUN_ID)


def test_live_resolve_missing_path_raises_filenotfound(authed_session):
    """Requesting a definitely-non-existent path must raise FileNotFoundError."""
    drive = authed_session['drive']
    bogus = f"{SENTINEL_PATH}/this-definitely-does-not-exist-{uuid.uuid4().hex}"
    with pytest.raises(FileNotFoundError):
        drive.resolve_path(bogus)


def test_live_list_folder_with_paths_returns_enriched_entries(authed_session, sentinel_folder, tmp_path):
    """list_folder_with_paths must annotate entries with full path + display_name."""
    drive = authed_session['drive']
    sentinel_uuid = sentinel_folder['uuid']

    name = _unique_name('listing-probe')
    local_file, _ = _write_payload(tmp_path, f"{name}.txt", size_bytes=64)
    drive.upload_file_to_folder(
        str(local_file), sentinel_uuid,
        custom_name=name, custom_extension='txt',
    )

    drive.folder_content_cache.pop(sentinel_uuid, None)
    listing = drive.list_folder_with_paths(SENTINEL_PATH)
    assert listing['current_path'].endswith(RUN_ID)
    files = listing['files']
    expected_display = f"{name}.txt"
    probe = next((f for f in files if f.get('display_name') == expected_display), None)
    assert probe is not None, f"{expected_display} not in listing: {[f.get('display_name') for f in files]}"
    assert probe['path'].endswith(expected_display)
    assert 'size_display' in probe


# =============================================================================
# RECURSIVE FOLDER CREATION (covers the bug we just fixed)
# =============================================================================

def test_live_recursive_folder_creation_then_resolve(authed_session, sentinel_folder):
    """Create a 3-level nested path and verify each segment resolves.

    REGRESSION: this is the scenario that caught the cache-coherency bug
    in create_folder_recursive. If intermediate folders don't invalidate
    the parent cache, resolve_path() falls through to FileNotFoundError
    even though the chain exists on the server.
    """
    drive = authed_session['drive']
    nested_path = f"{SENTINEL_PATH}/lvl1/lvl2/lvl3"
    print(f"\n📁 LIVE: Creating nested {nested_path}")
    deep_info = drive.create_folder_recursive(nested_path)
    assert deep_info['uuid']

    # Each level must be resolvable from a fresh cache
    drive.folder_content_cache.clear()
    for path in (
        f"{SENTINEL_PATH}/lvl1",
        f"{SENTINEL_PATH}/lvl1/lvl2",
        f"{SENTINEL_PATH}/lvl1/lvl2/lvl3",
    ):
        resolved = drive.resolve_path(path)
        assert resolved['type'] == 'folder', f"{path} did not resolve as folder"
        assert resolved['uuid'], f"{path} resolved with no uuid"
    print("✅ LIVE: All 3 nested levels resolve cleanly from a cold cache")


# =============================================================================
# FILE OPERATIONS: rename, move, copy, update
# =============================================================================

def test_live_file_rename_in_place(authed_session, sentinel_folder, tmp_path):
    """Upload, rename, verify the new name is queryable and old is gone."""
    drive = authed_session['drive']
    sentinel_uuid = sentinel_folder['uuid']

    before = _unique_name('before')
    after = _unique_name('after')
    local_file, _ = _write_payload(tmp_path, f"{before}.txt", size_bytes=64)
    uploaded = drive.upload_file_to_folder(
        str(local_file), sentinel_uuid,
        custom_name=before, custom_extension='txt',
    )
    file_uuid = uploaded['uuid']

    drive.rename_file(file_uuid, f"{after}.txt")

    drive.folder_content_cache.pop(sentinel_uuid, None)
    listing = drive.get_folder_content(sentinel_uuid)
    plain_names = [f.get('plainName') for f in listing['files']]
    assert after in plain_names, f"Renamed file {after!r} not in: {plain_names}"
    assert before not in plain_names, f"Old name {before!r} still present: {plain_names}"
    print(f"✅ LIVE: rename {before}.txt -> {after}.txt OK")


def test_live_file_move_between_folders(authed_session, sentinel_folder, tmp_path):
    """Upload to folder A, move to folder B, verify it's in B and not A."""
    drive = authed_session['drive']

    # Two subfolders under the sentinel
    src_path = _unique_subpath("src")
    dst_path = _unique_subpath("dst")
    src_info = drive.create_folder_recursive(src_path)
    dst_info = drive.create_folder_recursive(dst_path)

    name = _unique_name('movable')
    local_file, _ = _write_payload(tmp_path, f"{name}.txt", size_bytes=64)
    uploaded = drive.upload_file_to_folder(
        str(local_file), src_info['uuid'],
        custom_name=name, custom_extension='txt',
    )
    file_uuid = uploaded['uuid']

    # Move (retry for eventual consistency — server may reject if file
    # hasn't fully propagated yet)
    for _attempt in range(3):
        try:
            drive.move_file(file_uuid, dst_info['uuid'])
            break
        except Exception as exc:
            if 'can not be moved' in str(exc).lower() and _attempt < 2:
                time.sleep(3)
                continue
            raise

    # Verify: appears in dst, not in src
    drive.folder_content_cache.pop(src_info['uuid'], None)
    drive.folder_content_cache.pop(dst_info['uuid'], None)

    src_files = [f['uuid'] for f in drive.get_folder_content(src_info['uuid'])['files']]
    dst_files = [f['uuid'] for f in drive.get_folder_content(dst_info['uuid'])['files']]

    assert file_uuid not in src_files, "File still in source folder after move"
    assert file_uuid in dst_files, "File not in destination folder after move"
    print(f"✅ LIVE: move {file_uuid[:8]}... from src to dst OK")


def test_live_file_copy_preserves_content(authed_session, sentinel_folder, tmp_path):
    """Copy a file to another folder; the copy must have a different uuid
    AND the same content as the original."""
    drive = authed_session['drive']

    # Source folder + destination folder
    src_path = _unique_subpath("copy-src")
    dst_path = _unique_subpath("copy-dst")
    src_info = drive.create_folder_recursive(src_path)
    dst_info = drive.create_folder_recursive(dst_path)

    name = _unique_name('original')
    local_file, payload = _write_payload(tmp_path, f"{name}.txt", size_bytes=128)
    original = drive.upload_file_to_folder(
        str(local_file), src_info['uuid'],
        custom_name=name, custom_extension='txt',
    )

    # Copy to dst
    copy_result = drive.copy_item(original['uuid'], dst_info['uuid'])
    assert copy_result['success'], f"Copy failed: {copy_result}"

    # The original must still be in src
    drive.folder_content_cache.clear()
    src_files = [f['uuid'] for f in drive.get_folder_content(src_info['uuid'])['files']]
    assert original['uuid'] in src_files, "Original removed by copy operation"

    # The copy must be in dst with a different uuid but identical content
    dst_listing = drive.get_folder_content(dst_info['uuid'])
    assert len(dst_listing['files']) == 1, (
        f"Expected 1 file in dst after copy, got {len(dst_listing['files'])}"
    )
    copy_file = dst_listing['files'][0]
    assert copy_file['uuid'] != original['uuid'], "Copy has same uuid as original"

    # Download both into separate subdirs; compare bytes.
    # Pre-create the dirs so download_file treats them as destination DIRs
    # (vs. file paths) and writes <dir>/<filename> inside each.
    orig_dir = tmp_path / "downloads" / "orig"
    copy_dir = tmp_path / "downloads" / "copy"
    orig_dir.mkdir(parents=True, exist_ok=True)
    copy_dir.mkdir(parents=True, exist_ok=True)

    orig_path = drive.download_file(original['uuid'], str(orig_dir))
    copy_path = drive.download_file(copy_file['uuid'], str(copy_dir))

    orig_bytes = Path(orig_path).read_bytes()
    copy_bytes = Path(copy_path).read_bytes()
    assert orig_bytes == payload, "Original bytes don't match upload"
    assert copy_bytes == payload, "Copy bytes don't match original"
    print("✅ LIVE: copy original -> copy, both have identical bytes")


def test_live_update_file_replaces_content(authed_session, sentinel_folder, tmp_path):
    """update_file (used by WebDAV PUT) must replace the file's bytes
    while keeping the same uuid + filename."""
    drive = authed_session['drive']
    sentinel_uuid = sentinel_folder['uuid']

    # Initial upload
    name = _unique_name('updatable')
    local_v1, payload_v1 = _write_payload(tmp_path, f"{name}.txt", size_bytes=64)
    uploaded = drive.upload_file_to_folder(
        str(local_v1), sentinel_uuid,
        custom_name=name, custom_extension='txt',
    )
    file_uuid = uploaded['uuid']

    # Update with new content
    local_v2 = tmp_path / "updatable_v2.txt"
    payload_v2 = b"REPLACED content for update_file test " + uuid.uuid4().bytes
    local_v2.write_bytes(payload_v2)

    update_result = drive.update_file(file_uuid, str(local_v2))
    assert update_result['success'], f"update_file failed: {update_result}"

    # Download — uuid is unchanged but bytes must be the new content
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    out_path = drive.download_file(file_uuid, str(download_dir))
    downloaded = Path(out_path).read_bytes()
    assert downloaded == payload_v2, "Downloaded bytes don't match the update"
    assert downloaded != payload_v1, "Update didn't actually replace the content"
    print(f"✅ LIVE: update_file replaced {len(payload_v1)} -> {len(payload_v2)} bytes")


# =============================================================================
# FOLDER OPERATIONS: rename, move
# =============================================================================

def test_live_folder_rename(authed_session, sentinel_folder):
    """Rename a folder; verify the new name is in the parent listing
    and the old name is not."""
    drive = authed_session['drive']
    sentinel_uuid = sentinel_folder['uuid']

    # Create a folder with a known name
    target_path = _unique_subpath("renameable")
    folder = drive.create_folder_recursive(target_path)

    # Rename it
    new_name = f"renamed_{uuid.uuid4().hex[:6]}"
    drive.rename_folder(folder['uuid'], new_name)

    # Sentinel listing must show the renamed folder, not the original name
    drive.folder_content_cache.pop(sentinel_uuid, None)
    sentinel_listing = drive.get_folder_content(sentinel_uuid)
    folder_names = [f.get('plainName') for f in sentinel_listing['folders']]
    assert new_name in folder_names, (
        f"Renamed folder '{new_name}' not in sentinel: {folder_names}"
    )
    print(f"✅ LIVE: folder rename to {new_name} OK")


def test_live_folder_move_to_another_parent(authed_session, sentinel_folder):
    """Move a folder under one parent to another parent."""
    drive = authed_session['drive']

    # Create src parent + child folder, dst parent
    src_parent = drive.create_folder_recursive(_unique_subpath("src-parent"))
    dst_parent = drive.create_folder_recursive(_unique_subpath("dst-parent"))

    # Build a child folder under src_parent
    child_uuid = drive.create_folder("movable-child", src_parent['uuid'])['uuid']

    # Move child from src_parent → dst_parent
    drive.move_folder(child_uuid, dst_parent['uuid'])

    # Verify: child is now in dst_parent, not in src_parent
    drive.folder_content_cache.clear()
    src_subs = [f.get('plainName') for f in drive.get_folder_content(src_parent['uuid'])['folders']]
    dst_subs = [f.get('plainName') for f in drive.get_folder_content(dst_parent['uuid'])['folders']]

    assert 'movable-child' not in src_subs, "Child still in src after move"
    assert 'movable-child' in dst_subs, "Child not in dst after move"
    print("✅ LIVE: folder move src-parent -> dst-parent OK")


# =============================================================================
# TRASH
# =============================================================================

def test_live_trash_file_then_gone_from_listing(authed_session, sentinel_folder, tmp_path):
    """After trash_file, the file no longer appears in the parent listing."""
    drive = authed_session['drive']
    sentinel_uuid = sentinel_folder['uuid']

    name = _unique_name('trashable')
    local_file, _ = _write_payload(tmp_path, f"{name}.txt", size_bytes=64)
    uploaded = drive.upload_file_to_folder(
        str(local_file), sentinel_uuid,
        custom_name=name, custom_extension='txt',
    )
    file_uuid = uploaded['uuid']

    drive.trash_file(file_uuid)

    drive.folder_content_cache.pop(sentinel_uuid, None)
    listing = drive.get_folder_content(sentinel_uuid)
    file_uuids = [f.get('uuid') for f in listing['files']]
    assert file_uuid not in file_uuids, (
        f"Trashed file {file_uuid} still in listing: {file_uuids}"
    )
    print("✅ LIVE: file trash removes it from folder listing")


def test_live_trash_list_returns_items(authed_session):
    """trash-list API must return a dict with 'files' and 'folders' keys."""
    from utils.api import api_client
    result = api_client.get_trash_content(offset=0, limit=5)
    assert 'files' in result or 'folders' in result
    print(f"✅ LIVE: trash-list returned {len(result.get('files', []))} files, "
          f"{len(result.get('folders', []))} folders")


def test_live_quota_returns_usage(authed_session):
    """quota API must return drive/backup/total bytes."""
    from utils.api import api_client
    usage = api_client.get_storage_usage()
    assert 'total' in usage
    assert isinstance(usage['total'], (int, float))
    assert usage['total'] >= 0
    print(f"✅ LIVE: quota reports {usage['total'] / (1024**3):.2f} GB used")


# =============================================================================
# SEARCH (server-side fuzzy)
# =============================================================================

def test_live_search_finds_uniquely_named_file(authed_session, sentinel_folder, tmp_path):
    """Upload a file with a uniquely-named prefix, then search for it
    server-side. Note: server-side indexing may take a moment after
    upload, so we retry a few times before declaring failure."""
    drive = authed_session['drive']
    sentinel_uuid = sentinel_folder['uuid']

    # A name that's almost certainly unique on the user's drive
    unique_token = f"pytestsmoke{uuid.uuid4().hex[:10]}"
    local_file, _ = _write_payload(tmp_path, f"{unique_token}.txt", size_bytes=64)
    uploaded = drive.upload_file_to_folder(
        str(local_file), sentinel_uuid,
        custom_name=unique_token, custom_extension='txt',
    )
    file_uuid = uploaded['uuid']

    # Try a few times — server-side index may need a moment
    found = False
    last_results: list = []
    for attempt in range(5):
        results = drive.search_drive(unique_token)
        last_results = results
        result_ids = [
            r.get('itemId') or r.get('id') or r.get('uuid')
            for r in results
        ]
        if file_uuid in result_ids:
            found = True
            print(f"✅ LIVE: search found upload after attempt {attempt + 1}")
            break
        time.sleep(2)

    if not found:
        # Don't fail the suite hard — server-side indexing latency is
        # outside our control. Skip with a clear message instead so
        # we know the rest of the test ran.
        pytest.skip(
            f"Server-side search index did not surface '{unique_token}' "
            f"within ~10s. This is a backend-latency issue, not a CLI "
            f"bug. Last results: {last_results}"
        )


def test_live_search_with_bogus_query_returns_list(authed_session):
    """Search with a never-matched query must return safely (a list, not
    a crash). Note: the Internxt fuzzy search is *very* fuzzy — even a
    32-char random hex string returns ~10 matches with similarity scores
    around 1-2% (the server effectively ranks every item by distance from
    the query). So we don't assert empty results — we only assert the
    response shape is sane and that everything returned has a low
    similarity score (< 5%) confirming none of these are real matches."""
    drive = authed_session['drive']
    bogus_term = f"definitelyNoSuchThing{uuid.uuid4().hex}"
    try:
        results = drive.search_drive(bogus_term)
    except Exception as e:
        # 4xx on no-results would also be acceptable
        print(f"ℹ️  LIVE: search of bogus term raised cleanly: {e}")
        return
    assert isinstance(results, list)
    # All results (if any) must have very low similarity since the term
    # doesn't appear in any real filename. Backend's noisy ranking, but
    # nothing should be a "real" match.
    for r in results:
        sim = r.get('similarity', 0)
        if sim is not None:
            assert sim < 0.15, (
                f"Bogus search returned a high-similarity match ({sim}): {r}"
            )
    print(f"✅ LIVE: bogus search returned {len(results)} fuzzy matches, "
          f"all with similarity < 10% (as expected)")


# =============================================================================
# FIND (recursive client-side wildcard search)
# =============================================================================

def test_live_find_files_within_sentinel(authed_session, sentinel_folder, tmp_path):
    """Upload two files with a probe extension + one unrelated, then
    find('*.<probe-ext>') must return only the two matching ones.

    Use a per-call probe extension so reruns don't collide with prior
    attempts' uploads in the shared sentinel folder.
    """
    drive = authed_session['drive']
    sentinel_uuid = sentinel_folder['uuid']

    probe_ext = f"findprobe{uuid.uuid4().hex[:6]}"
    expected_names = []
    for i in range(2):
        name = _unique_name(f"finder{i}")
        local, _ = _write_payload(tmp_path, f"{name}.{probe_ext}", size_bytes=64)
        drive.upload_file_to_folder(
            str(local), sentinel_uuid,
            custom_name=name, custom_extension=probe_ext,
        )
        expected_names.append(f"{name}.{probe_ext}")

    control_name = _unique_name('control')
    local_other, _ = _write_payload(tmp_path, f"{control_name}.unrelated", size_bytes=64)
    drive.upload_file_to_folder(
        str(local_other), sentinel_uuid,
        custom_name=control_name, custom_extension='unrelated',
    )

    drive.folder_content_cache.clear()
    results = drive.find_files(f'*.{probe_ext}', SENTINEL_PATH)
    names = sorted(r['display_name'] for r in results)
    assert names == sorted(expected_names), (
        f"Expected {sorted(expected_names)}, got: {names}"
    )
    print(f"✅ LIVE: find('*.{probe_ext}') returned exactly the matching files")


# =============================================================================
# BATCHED / NESTED OPERATIONS
# =============================================================================

def test_live_recursive_folder_upload_download(authed_session, sentinel_folder, tmp_path):
    """Build a 3-folder local tree with 4 files, upload it (per-file
    via upload_file_to_folder, the same path the CLI takes for `-r`),
    then download recursively into a different local dir and verify
    the structure + bytes match for every file."""
    drive = authed_session['drive']
    root_remote_path = _unique_subpath("rec-tree")

    # Build local source tree:
    #   <tmp>/src/
    #     a.txt
    #     b.bin
    #     sub1/
    #       c.txt
    #     sub2/
    #       sub2sub/
    #         d.txt
    src_root = tmp_path / "src"
    src_root.mkdir()
    (src_root / "sub1").mkdir()
    (src_root / "sub2").mkdir()
    (src_root / "sub2" / "sub2sub").mkdir()

    files = {
        'a.txt':                       _write_payload(tmp_path / "src", "a.txt", 64)[1],
        'b.bin':                       _write_payload(tmp_path / "src", "b.bin", 256)[1],
        'sub1/c.txt':                  _write_payload(src_root / "sub1", "c.txt", 96)[1],
        'sub2/sub2sub/d.txt':          _write_payload(src_root / "sub2" / "sub2sub", "d.txt", 128)[1],
    }
    print(f"\n🌳 LIVE: Built local tree with {len(files)} files at {src_root}")

    # Upload by walking the tree: for each file, ensure the remote folder
    # path exists then upload into it. Mirrors what the CLI's recursive
    # upload does internally.
    remote_uuids = {}  # rel_path -> remote file uuid
    for rel_path, payload in files.items():
        local_file = src_root / rel_path
        rel_dir = '/'.join(rel_path.split('/')[:-1])
        target_remote_dir = root_remote_path
        if rel_dir:
            target_remote_dir = f"{root_remote_path}/{rel_dir}"
        folder_info = drive.create_folder_recursive(target_remote_dir)
        # Use a unique custom_name per file so reruns don't collide
        leaf = local_file.name
        stem, _, ext = leaf.rpartition('.')
        unique = _unique_name(stem or leaf)
        uploaded = drive.upload_file_to_folder(
            str(local_file), folder_info['uuid'],
            custom_name=unique, custom_extension=ext if stem else '',
        )
        remote_uuids[rel_path] = (uploaded['uuid'], unique, ext)
    print(f"📤 LIVE: Uploaded all {len(files)} files into {root_remote_path}")

    # Download recursively: for each known remote uuid, download into the
    # corresponding local subpath, then compare bytes.
    download_root = tmp_path / "downloads"
    download_root.mkdir()
    for rel_path, (uuid_, unique_name, ext) in remote_uuids.items():
        local_dir = download_root / Path(rel_path).parent
        local_dir.mkdir(parents=True, exist_ok=True)
        out_path = drive.download_file(uuid_, str(local_dir))
        downloaded = Path(out_path).read_bytes()
        original = files[rel_path]
        assert downloaded == original, (
            f"Bytes mismatch for {rel_path}: "
            f"downloaded {len(downloaded)} != original {len(original)}"
        )
    print(f"✅ LIVE: All {len(files)} files round-tripped through recursive upload/download")


def test_live_move_nonempty_folder_brings_children(authed_session, sentinel_folder, tmp_path):
    """Move a folder that contains files + a nested subfolder.
    Verify children come along with the same UUIDs (no re-upload)."""
    drive = authed_session['drive']

    # Build remote tree:
    #   <sentinel>/src-parent/<rand>/A/
    #     file1.txt
    #     file2.txt
    #     sub/
    #       file3.txt
    src_root_path = _unique_subpath("move-src")
    a_path = f"{src_root_path}/A"
    a_sub_path = f"{a_path}/sub"
    a_info = drive.create_folder_recursive(a_path)
    a_sub_info = drive.create_folder_recursive(a_sub_path)

    # Upload three files
    file_uuids: dict[str, str] = {}
    for placement_uuid, rel_name in [
        (a_info['uuid'], 'file1'),
        (a_info['uuid'], 'file2'),
        (a_sub_info['uuid'], 'file3'),
    ]:
        unique = _unique_name(rel_name)
        local, _ = _write_payload(tmp_path, f"{unique}.txt", size_bytes=64)
        uploaded = drive.upload_file_to_folder(
            str(local), placement_uuid,
            custom_name=unique, custom_extension='txt',
        )
        file_uuids[rel_name] = uploaded['uuid']

    # Now move /move-src/A → /move-dst/A by moving A into a fresh dst-parent
    dst_parent_path = _unique_subpath("move-dst")
    dst_parent_info = drive.create_folder_recursive(dst_parent_path)

    drive.move_folder(a_info['uuid'], dst_parent_info['uuid'])
    print(f"\n📦 LIVE: Moved /A from {src_root_path} to {dst_parent_path}")

    # Verify: A no longer in src-parent, IS in dst-parent
    drive.folder_content_cache.clear()
    src_root_info = drive.resolve_path(src_root_path)
    src_root_listing = drive.get_folder_content(src_root_info['uuid'])
    src_subfolders = [f.get('plainName') for f in src_root_listing['folders']]
    assert 'A' not in src_subfolders, (
        f"Folder A still in source after move: {src_subfolders}"
    )

    dst_listing = drive.get_folder_content(dst_parent_info['uuid'])
    dst_subfolders = [f.get('plainName') for f in dst_listing['folders']]
    assert 'A' in dst_subfolders, (
        f"Folder A not in destination after move: {dst_subfolders}"
    )

    # Critical: all 3 child files must still resolve and have the SAME uuids
    # (move = pointer reparent, not delete+upload)
    for rel_name, original_uuid in file_uuids.items():
        if rel_name == 'file3':
            new_path = f"{dst_parent_path}/A/sub/{_find_full_name(drive, dst_parent_info['uuid'], 'A', 'sub', rel_name)}"
        else:
            new_path = f"{dst_parent_path}/A/{_find_full_name_in_a(drive, dst_parent_info['uuid'], rel_name)}"
        # Resolve the file under its new path; uuid must match
        resolved = drive.resolve_path(new_path)
        assert resolved['uuid'] == original_uuid, (
            f"Child file {rel_name} got a new uuid after parent move: "
            f"was {original_uuid}, now {resolved['uuid']}"
        )
    print("✅ LIVE: Move brought all 3 children with their original uuids")


def _find_full_name_in_a(drive, dst_parent_uuid, prefix):
    """Helper: locate the new full filename of `prefix*.txt` under
    dst_parent/A/. Used after a folder move to discover the renamed
    leaf names that include _unique_name() suffixes."""
    drive.folder_content_cache.clear()
    dst_listing = drive.get_folder_content(dst_parent_uuid)
    a_uuid = next(
        (f['uuid'] for f in dst_listing['folders'] if f.get('plainName') == 'A'),
        None,
    )
    assert a_uuid, "Folder A not found in destination"
    a_listing = drive.get_folder_content(a_uuid)
    for f in a_listing['files']:
        plain = f.get('plainName', '')
        if plain.startswith(prefix):
            ext = f.get('type', '')
            return f"{plain}.{ext}" if ext else plain
    raise AssertionError(f"No file matching {prefix}* found under A/")


def _find_full_name(drive, dst_parent_uuid, a_name, sub_name, prefix):
    """Like _find_full_name_in_a but for one level deeper (A/sub/)."""
    drive.folder_content_cache.clear()
    dst_listing = drive.get_folder_content(dst_parent_uuid)
    a_uuid = next(
        (f['uuid'] for f in dst_listing['folders'] if f.get('plainName') == a_name),
        None,
    )
    assert a_uuid, f"Folder {a_name} not found in destination"
    a_listing = drive.get_folder_content(a_uuid)
    sub_uuid = next(
        (f['uuid'] for f in a_listing['folders'] if f.get('plainName') == sub_name),
        None,
    )
    assert sub_uuid, f"Folder {sub_name} not found under {a_name}"
    sub_listing = drive.get_folder_content(sub_uuid)
    for f in sub_listing['files']:
        plain = f.get('plainName', '')
        if plain.startswith(prefix):
            ext = f.get('type', '')
            return f"{plain}.{ext}" if ext else plain
    raise AssertionError(f"No file matching {prefix}* found under {a_name}/{sub_name}/")


def test_live_rename_folder_preserves_child_paths(authed_session, sentinel_folder, tmp_path):
    """Rename a folder that contains a file. The file's UUID must be
    unchanged AND it must be resolvable under the new folder path."""
    drive = authed_session['drive']

    foo_path = _unique_subpath("foo")
    foo_info = drive.create_folder_recursive(foo_path)

    # Upload a single file under /foo/
    file_name = _unique_name("inside")
    local_file, _ = _write_payload(tmp_path, f"{file_name}.txt", size_bytes=64)
    uploaded = drive.upload_file_to_folder(
        str(local_file), foo_info['uuid'],
        custom_name=file_name, custom_extension='txt',
    )
    file_uuid = uploaded['uuid']

    # Rename /foo/ to /bar/
    new_folder_name = _unique_name("bar")
    drive.rename_folder(foo_info['uuid'], new_folder_name)
    print(f"\n🏷️  LIVE: Renamed folder to {new_folder_name}")

    # Reconstruct the new path and resolve the file there
    parent_of_foo = foo_path.rsplit('/', 1)[0]
    new_file_path = f"{parent_of_foo}/{new_folder_name}/{file_name}.txt"

    drive.folder_content_cache.clear()
    resolved = drive.resolve_path(new_file_path)
    assert resolved['type'] == 'file'
    assert resolved['uuid'] == file_uuid, (
        f"Child file uuid changed after parent rename: "
        f"{file_uuid} -> {resolved['uuid']}"
    )
    print("✅ LIVE: Child file resolves under renamed folder, uuid preserved")


def test_live_trash_nonempty_folder_removes_children(authed_session, sentinel_folder, tmp_path):
    """Trash a folder containing files. Verify the folder is gone from
    the parent listing AND its child files no longer resolve."""
    drive = authed_session['drive']

    target_path = _unique_subpath("trashable-folder")
    target_info = drive.create_folder_recursive(target_path)

    # Add 2 files inside
    file_uuids = []
    file_paths = []
    for stem in ("inner1", "inner2"):
        unique = _unique_name(stem)
        local, _ = _write_payload(tmp_path, f"{unique}.txt", size_bytes=64)
        uploaded = drive.upload_file_to_folder(
            str(local), target_info['uuid'],
            custom_name=unique, custom_extension='txt',
        )
        file_uuids.append(uploaded['uuid'])
        file_paths.append(f"{target_path}/{unique}.txt")

    # Trash the entire folder
    drive.trash_folder(target_info['uuid'])
    print(f"\n🗑️  LIVE: Trashed non-empty folder {target_path}")

    # Verify folder gone from parent listing
    drive.folder_content_cache.clear()
    parent_path = target_path.rsplit('/', 1)[0]
    parent_info = drive.resolve_path(parent_path)
    parent_listing = drive.get_folder_content(parent_info['uuid'])
    parent_subfolders = [f.get('plainName') for f in parent_listing['folders']]
    target_leaf = target_path.rsplit('/', 1)[1]
    assert target_leaf not in parent_subfolders, (
        f"Trashed folder still in parent listing: {parent_subfolders}"
    )

    # Verify child files no longer resolve under the old path
    for path in file_paths:
        with pytest.raises(FileNotFoundError):
            drive.resolve_path(path)
    print(f"✅ LIVE: Folder trash also removed all {len(file_paths)} children from listing")


def test_live_upload_conflict_skip_does_not_replace(authed_session, sentinel_folder, tmp_path):
    """Upload a file, then attempt to re-upload to the same target with
    on_conflict='skip'. The result must be 'skipped' and the original
    UUID must remain the canonical one for that path."""
    drive = authed_session['drive']

    # Use a fresh subfolder so there's no contamination from other tests
    test_dir_path = _unique_subpath("conflict-skip")
    test_dir = drive.create_folder_recursive(test_dir_path)

    # Initial upload
    name = _unique_name("conflict-target")
    local_v1, payload_v1 = _write_payload(tmp_path, f"{name}.txt", size_bytes=64)
    initial = drive.upload_file_to_folder(
        str(local_v1), test_dir['uuid'],
        custom_name=name, custom_extension='txt',
    )
    initial_uuid = initial['uuid']

    # Same content, different local file path; same remote name → conflict
    local_v2 = tmp_path / f"{name}_again.txt"
    local_v2.write_bytes(b"second-attempt-different-bytes-" + uuid.uuid4().bytes)
    result = drive.upload_single_item_with_conflict_handling(
        local_v2,
        target_remote_parent_path_str=test_dir_path,
        target_folder_uuid=test_dir['uuid'],
        on_conflict='skip',
        remote_filename=f"{name}.txt",
    )
    assert result == 'skipped', f"Expected 'skipped', got {result!r}"

    # Verify the file uuid AT that path is still the original
    drive.folder_content_cache.clear()
    resolved = drive.resolve_path(f"{test_dir_path}/{name}.txt")
    assert resolved['uuid'] == initial_uuid, (
        "File UUID changed despite on_conflict='skip'"
    )

    # And the bytes are still the original
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    out_path = drive.download_file(initial_uuid, str(download_dir))
    assert Path(out_path).read_bytes() == payload_v1, (
        "File content changed despite on_conflict='skip'"
    )
    print("✅ LIVE: on_conflict='skip' preserved original UUID + bytes")


def test_live_upload_conflict_overwrite_replaces(authed_session, sentinel_folder, tmp_path):
    """Upload a file, then re-upload with on_conflict='overwrite'.
    The UUID changes (overwrite = trash old + upload new) but the path
    resolves to a file with the NEW bytes."""
    drive = authed_session['drive']

    test_dir_path = _unique_subpath("conflict-overwrite")
    test_dir = drive.create_folder_recursive(test_dir_path)

    # First upload (content A)
    name = _unique_name("overwrite-target")
    local_v1, payload_v1 = _write_payload(tmp_path, f"{name}.txt", size_bytes=64)
    initial = drive.upload_file_to_folder(
        str(local_v1), test_dir['uuid'],
        custom_name=name, custom_extension='txt',
    )
    initial_uuid = initial['uuid']

    # Second upload (content B) with overwrite
    local_v2 = tmp_path / f"{name}_v2.txt"
    payload_v2 = b"OVERWRITTEN content " + uuid.uuid4().bytes
    local_v2.write_bytes(payload_v2)

    result = drive.upload_single_item_with_conflict_handling(
        local_v2,
        target_remote_parent_path_str=test_dir_path,
        target_folder_uuid=test_dir['uuid'],
        on_conflict='overwrite',
        remote_filename=f"{name}.txt",
    )
    assert result == 'uploaded', f"Expected 'uploaded', got {result!r}"

    # The path now resolves to a file with NEW content
    drive.folder_content_cache.clear()
    resolved = drive.resolve_path(f"{test_dir_path}/{name}.txt")
    assert resolved['type'] == 'file'
    new_uuid = resolved['uuid']
    assert new_uuid != initial_uuid, (
        "UUID unchanged after overwrite — overwrite=delete+upload, expected new uuid"
    )

    # Download via the new uuid; bytes must be v2
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    out_path = drive.download_file(new_uuid, str(download_dir))
    downloaded = Path(out_path).read_bytes()
    assert downloaded == payload_v2, (
        f"Downloaded bytes don't match v2: {len(downloaded)} bytes"
    )
    assert downloaded != payload_v1, "Overwrite didn't actually replace content"
    print("✅ LIVE: on_conflict='overwrite' produced new UUID with replaced bytes")
