"""Live integration smoke test against the real Internxt backend.

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

To run:
    pytest tests/test_live_smoke.py -v -s

To force-skip even with creds present:
    PYTEST_SKIP_LIVE=1 pytest tests/test_live_smoke.py
"""
import os
import sys
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

pytestmark = pytest.mark.skipif(
    _SKIP_REASON is not None,
    reason=_SKIP_REASON or "",
)


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
        drive_service.trash_by_path(SENTINEL_PATH)
        print("✅ LIVE: Cleanup successful (sent to trash)")
    except FileNotFoundError:
        print("ℹ️  LIVE: Sentinel folder already gone")
    except Exception as e:
        print(f"⚠️  LIVE: Cleanup failed (please manually trash {SENTINEL_PATH}): {e}")


# ---------- read-only smoke (always safe) ----------

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


# ---------- end-to-end: create folder → upload → download → trash ----------

def test_live_full_upload_download_cycle(authed_session, tmp_path):
    """The headline live test:
       1. Create the sentinel folder
       2. Generate a small random payload locally
       3. Upload it into the sentinel folder
       4. List the sentinel folder, verify the file appears
       5. Download it to a tmp dir
       6. Assert downloaded bytes == original bytes
       (Cleanup happens in the module-scope fixture's teardown.)
    """
    drive = authed_session['drive']

    # 1. Create sentinel folder (recursive — handles nested path)
    print(f"\n📁 LIVE: Creating sentinel folder {SENTINEL_PATH}")
    folder_info = drive.create_folder_recursive(SENTINEL_PATH)
    sentinel_uuid = folder_info['uuid']
    assert sentinel_uuid
    print(f"✅ LIVE: Sentinel folder uuid={sentinel_uuid[:8]}...")

    # 2. Generate payload + write to disk
    payload = b"internxt-cli pytest smoke " + uuid.uuid4().bytes + b"\n" * 10
    local_file = tmp_path / "smoke.txt"
    local_file.write_bytes(payload)
    print(f"📝 LIVE: Wrote {len(payload)} bytes to {local_file}")

    # 3. Upload
    print("📤 LIVE: Uploading...")
    uploaded = drive.upload_file_to_folder(
        str(local_file), sentinel_uuid,
        custom_name='smoke', custom_extension='txt',
    )
    file_uuid = uploaded.get('uuid')
    assert file_uuid, f"Upload returned no uuid: {uploaded}"
    print(f"✅ LIVE: Uploaded uuid={file_uuid[:8]}...")

    # 4. List the sentinel folder
    print("📋 LIVE: Listing sentinel folder...")
    drive.folder_content_cache.pop(sentinel_uuid, None)  # force fresh fetch
    listing = drive.get_folder_content(sentinel_uuid)
    file_uuids = [f.get('uuid') for f in listing['files']]
    assert file_uuid in file_uuids, (
        f"Uploaded file {file_uuid} not in folder listing: {file_uuids}"
    )
    print("✅ LIVE: File appears in folder listing")

    # 5. Download
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    print("📥 LIVE: Downloading...")
    out_path = drive.download_file(file_uuid, str(download_dir))

    # 6. Byte-for-byte verification
    downloaded = Path(out_path).read_bytes()
    assert downloaded == payload, (
        f"Downloaded {len(downloaded)} bytes != uploaded {len(payload)} bytes"
    )
    print("✅ LIVE: Downloaded bytes match upload exactly")


def test_live_path_resolution_works(authed_session):
    """resolve_path against the live API for the sentinel folder we created."""
    drive = authed_session['drive']
    # Note: depends on the previous test having created the folder.
    # Tests share module scope so this should always work after the cycle test.
    try:
        resolved = drive.resolve_path(SENTINEL_PATH)
        assert resolved['type'] == 'folder'
        assert resolved['path'].endswith(RUN_ID)
        print(f"\n✅ LIVE: resolve_path returned uuid={resolved['uuid'][:8]}...")
    except FileNotFoundError:
        pytest.skip("Sentinel folder not created (prior test may have failed)")
