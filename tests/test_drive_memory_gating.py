"""Tests for drive_service memory-gated upload concurrency helpers
and _get_network_auth credential builder.

These cover the safety-margin reservation algorithm that prevents OOM
during parallel uploads.
"""
import threading
from unittest.mock import patch

import pytest

from services.drive import drive_service


@pytest.fixture(autouse=True)
def _reset_mem_state():
    """Ensure each test starts with no outstanding memory reservations."""
    drive_service._mem_reserved = 0
    yield
    drive_service._mem_reserved = 0


# ---------- _available_memory ----------

def test_available_memory_returns_int_above_zero():
    """Cross-platform best-effort memory probe must always return a positive int."""
    n = drive_service._available_memory()
    assert isinstance(n, int)
    assert n > 0


def test_available_memory_falls_back_to_4gb_when_all_probes_fail():
    """If psutil is missing AND platform-specific probes fail, return 4 GB."""
    with patch.dict('sys.modules', {'psutil': None}), \
         patch('services.drive.sys.platform', 'unknown-os'):
        # Force ImportError on psutil
        import sys
        sys.modules.pop('psutil', None)
        sys.modules['psutil'] = None
        try:
            n = drive_service._available_memory()
        finally:
            sys.modules.pop('psutil', None)
    assert n == 4 * 1024 * 1024 * 1024


# ---------- _mem_acquire / _mem_release ----------

def test_mem_acquire_when_plenty_available_returns_immediately():
    """With ample free memory, acquire must not block."""
    BIG = 10 * 1024 * 1024 * 1024  # claim 10 GB available
    with patch.object(drive_service, '_available_memory', return_value=BIG):
        drive_service._mem_acquire(100 * 1024 * 1024)  # 100 MB ask
    assert drive_service._mem_reserved == 100 * 1024 * 1024
    drive_service._mem_release(100 * 1024 * 1024)
    assert drive_service._mem_reserved == 0


def test_mem_release_does_not_underflow():
    """Releasing more than reserved must clamp to zero, not go negative."""
    drive_service._mem_release(1_000_000_000)
    assert drive_service._mem_reserved == 0


def test_mem_acquire_grants_first_worker_even_when_memory_tight():
    """Anti-deadlock: if avail<<need but no other worker holds memory,
    let one through (the OS may reclaim caches)."""
    # Tiny amount available, big request
    with patch.object(drive_service, '_available_memory',
                      return_value=10 * 1024):  # 10 KB available
        drive_service._mem_acquire(5 * 1024 * 1024 * 1024)  # 5 GB request
    assert drive_service._mem_reserved > 0
    drive_service._mem_release(5 * 1024 * 1024 * 1024)


def test_mem_acquire_blocks_then_releases():
    """If memory is tight AND another reservation exists, the second
    acquire must block until release wakes it."""
    SAFETY = 1 * 1024 * 1024 * 1024  # matches the constant inside

    # Pretend we have 2 GB available (so headroom = 1 GB after safety margin).
    AVAIL = SAFETY + 1024 * 1024 * 1024  # 2 GB
    with patch.object(drive_service, '_available_memory', return_value=AVAIL):
        # First worker takes the entire 1 GB headroom
        drive_service._mem_acquire(1024 * 1024 * 1024)

        # Second worker tries to take another 512 MB — should block.
        second_done = threading.Event()
        second_started = threading.Event()

        def second_worker():
            second_started.set()
            drive_service._mem_acquire(512 * 1024 * 1024)
            second_done.set()

        t = threading.Thread(target=second_worker, daemon=True)
        t.start()
        second_started.wait(timeout=1)
        # Within a brief window, second worker should NOT have completed
        assert not second_done.wait(timeout=0.2), "second worker should be blocked"

        # Release the first worker — this should unblock the second
        drive_service._mem_release(1024 * 1024 * 1024)

        # Now second worker should complete promptly
        assert second_done.wait(timeout=2), "second worker did not unblock"

        # Cleanup
        drive_service._mem_release(512 * 1024 * 1024)


# ---------- _get_network_auth ----------

def test_get_network_auth_builds_basic_auth_tuple():
    creds = {'bridgeUser': 'cli@example.com', 'userId': 'user-uuid-42'}
    user, password = drive_service._get_network_auth(creds)
    assert user == 'cli@example.com'
    # Password is sha256(user_id) per Internxt protocol
    import hashlib
    assert password == hashlib.sha256(b'user-uuid-42').hexdigest()


def test_get_network_auth_raises_on_missing_bridge_user():
    with pytest.raises(ValueError, match="Missing network credentials"):
        drive_service._get_network_auth({'userId': 'u'})


def test_get_network_auth_raises_on_missing_user_id():
    with pytest.raises(ValueError, match="Missing network credentials"):
        drive_service._get_network_auth({'bridgeUser': 'u@ex.com'})
