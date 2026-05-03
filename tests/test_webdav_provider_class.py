"""Tests for InternxtDAVProvider class — the main DAVProvider entry point."""
from unittest.mock import patch, MagicMock

import pytest

from services.webdav_provider import (
    InternxtDAVProvider,
    InternxtDAVCollection,
    webdav_api,
)


@pytest.fixture
def provider():
    """Build a provider without invoking webdav_api credential lookup."""
    fake_creds = {'user': {'email': 'u@example.com', 'rootFolderId': 'root-uuid'}}
    with patch.object(webdav_api, 'get_credentials', return_value=fake_creds):
        p = InternxtDAVProvider(preserve_timestamps=True)
    return p


# ---------- __init__ ----------

def test_provider_initializes_with_preserve_timestamps_flag():
    fake_creds = {'user': {'email': 'u@example.com'}}
    with patch.object(webdav_api, 'get_credentials', return_value=fake_creds):
        p_on = InternxtDAVProvider(preserve_timestamps=True)
        p_off = InternxtDAVProvider(preserve_timestamps=False)
    assert p_on.preserve_timestamps is True
    assert p_off.preserve_timestamps is False
    assert p_on.readonly is False


def test_provider_init_propagates_credential_failure():
    """If credentials can't be loaded, init must fail loudly."""
    with patch.object(webdav_api, 'get_credentials',
                      side_effect=ValueError("MissingCredentialsError")):
        with pytest.raises(ValueError, match="MissingCredentialsError"):
            InternxtDAVProvider()


# ---------- get_resource_inst ----------

def test_get_resource_inst_root_returns_collection(provider):
    env = {'wsgidav.provider': provider}
    result = provider.get_resource_inst('/', env)
    assert isinstance(result, InternxtDAVCollection)
    assert result.path == '/'


def test_get_resource_inst_normalizes_path_without_leading_slash(provider):
    env = {'wsgidav.provider': provider}
    # Stub the parent collection's get_member so we don't actually fetch
    with patch.object(InternxtDAVCollection, 'get_member', return_value=None):
        provider.get_resource_inst('Documents/foo.txt', env)
    # No exception → path normalization worked


def test_get_resource_inst_returns_none_for_recently_deleted(provider):
    """Recently-deleted paths short-circuit to None to avoid stale lookups."""
    env = {'wsgidav.provider': provider}
    webdav_api.mark_deleted('/just-deleted.txt')
    try:
        result = provider.get_resource_inst('/just-deleted.txt', env)
        assert result is None
    finally:
        # Reset to avoid leakage to other tests
        webdav_api._deleted_items.discard('/just-deleted.txt')


def test_get_resource_inst_returns_member_when_parent_has_it(provider):
    """For non-root paths, build parent collection then ask it for the member."""
    env = {'wsgidav.provider': provider}
    fake_member = MagicMock()

    with patch.object(InternxtDAVCollection, 'get_member',
                      return_value=fake_member):
        result = provider.get_resource_inst('/Documents/file.txt', env)
    assert result is fake_member


def test_get_resource_inst_returns_none_when_member_missing(provider):
    env = {'wsgidav.provider': provider}
    with patch.object(InternxtDAVCollection, 'get_member', return_value=None):
        result = provider.get_resource_inst('/no-such-file.txt', env)
    assert result is None


def test_get_resource_inst_returns_none_on_exception(provider):
    """Any error during lookup must be swallowed (returns None) so wsgidav
    can render a clean 404."""
    env = {'wsgidav.provider': provider}
    with patch.object(InternxtDAVCollection, 'get_member',
                      side_effect=ConnectionError("net")):
        result = provider.get_resource_inst('/x', env)
    assert result is None


# ---------- exists() ----------

def test_exists_returns_true_when_resource_found(provider):
    env = {'wsgidav.provider': provider}
    with patch.object(provider, 'get_resource_inst', return_value=MagicMock()):
        assert provider.exists('/path', env) is True


def test_exists_returns_false_when_resource_missing(provider):
    env = {'wsgidav.provider': provider}
    with patch.object(provider, 'get_resource_inst', return_value=None):
        assert provider.exists('/path', env) is False


def test_exists_returns_false_on_lookup_exception(provider):
    env = {'wsgidav.provider': provider}
    with patch.object(provider, 'get_resource_inst',
                      side_effect=RuntimeError("boom")):
        assert provider.exists('/path', env) is False
