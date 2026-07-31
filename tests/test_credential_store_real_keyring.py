"""Opt-in integration test for the production OS-keyring path.

This is deliberately excluded from the normal suite because it touches the
developer's real keychain.  Run it explicitly with
``INTERNXT_TEST_REAL_KEYRING=1 pytest -q tests/test_credential_store_real_keyring.py``.
The test uses a unique service and account and always removes the credential.
"""

import json
import os
import uuid

import pytest

import config.config as config_module
from config.config import ConfigService, CRED_FMT


@pytest.mark.skipif(
    os.environ.get("INTERNXT_TEST_REAL_KEYRING") != "1",
    reason="set INTERNXT_TEST_REAL_KEYRING=1 to touch the production OS keyring",
)
def test_real_keyring_missing_entry_is_created_and_round_trips(tmp_path, monkeypatch):
    service = f"internxt-cli-test-{uuid.uuid4().hex}"
    account = f"wrapping-key-{uuid.uuid4().hex}"
    monkeypatch.setattr(config_module, "KEYRING_SERVICE", service)
    monkeypatch.setattr(config_module, "KEYRING_KEY", account)
    monkeypatch.delenv("INTERNXT_NO_KEYRING", raising=False)

    client = ConfigService()
    client.internxt_cli_data_dir = tmp_path
    client.credentials_file = tmp_path / ".inxtcli"
    keyring = client._keyring()
    if keyring is None:
        pytest.skip("no usable production keyring backend is available")

    # Prove the account starts absent.  The following save must create it.
    assert keyring.get_password(service, account) is None
    try:
        client.save_user_credentials({"token": "real-keyring-test"})
        envelope = json.loads(client.credentials_file.read_text())
        assert envelope["fmt"] == CRED_FMT
        assert envelope["src"] == "keyring"
        assert keyring.get_password(service, account)
        assert client.read_user_credentials()["token"] == "real-keyring-test"
    finally:
        client.clear_user_credentials()
        assert keyring.get_password(service, account) is None
