"""Pytest config — make project root importable so `services.*` works."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture(autouse=True)
def _no_os_keyring(monkeypatch):
    """Never touch the real OS keychain during tests (no prompts, no pollution).

    Credential-store tests that want to exercise the keyring path opt back in by
    deleting this env var and injecting a fake keyring.
    """
    monkeypatch.setenv("INTERNXT_NO_KEYRING", "1")
    # Don't let a developer's shell env leak real creds into login tests.
    for var in ("INTERNXT_EMAIL", "INTERNXT_PASSWORD", "INTERNXT_CREDENTIALS_KEY"):
        monkeypatch.delenv(var, raising=False)
