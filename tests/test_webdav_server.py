"""Tests for services/webdav_server.py."""
from unittest.mock import MagicMock, patch

import pytest

from services.webdav_server import WebDAVServer


@pytest.fixture
def server():
    return WebDAVServer()


def test_invalid_server_choice_does_not_unbound_local(server):
    """Regression: `active_server` used to be referenced unbound when
    server_choice was anything other than auto/waitress/cheroot, raising
    UnboundLocalError. Now it raises ValueError (caught by start()'s wrapper
    and returned as success=False)."""
    with patch('services.webdav_server.WsgiDAVApp') as fake_app:
        fake_app.return_value = MagicMock()
        result = server.start(port=8181, server_choice='this-is-not-a-real-choice')
    assert result['success'] is False
    # The error message must come from the explicit ValueError, not from
    # an UnboundLocalError or NameError leak.
    msg = result.get('message', '')
    assert "Unknown server choice" in msg or "this-is-not-a-real-choice" in msg


def _capture_wsgi_app(host_config_value):
    """Helper: spin up start() with mocks; return the host passed to WsgiDAVApp."""
    from config.config import config_service
    from services.webdav_provider import InternxtDAVProvider

    captured = {}

    class FakeApp:
        def __init__(self, cfg):
            captured['config'] = cfg

    cfg_value = {} if host_config_value is None else {'host': host_config_value}

    # Patch:
    #  - read_webdav_config so we can inject host
    #  - WsgiDAVApp so we capture config without touching wsgidav internals
    #  - waitress.serve at the source module so the in-function `from waitress
    #    import serve` resolves to our stub (raise to short-circuit start())
    #  - InternxtDAVProvider.__init__ to skip auth setup
    with patch.object(config_service, 'read_webdav_config', return_value=cfg_value), \
         patch('wsgidav.wsgidav_app.WsgiDAVApp', FakeApp), \
         patch('waitress.serve', side_effect=RuntimeError("stop here")), \
         patch.object(InternxtDAVProvider, '__init__', return_value=None), \
         patch('services.webdav_server.WSGI_SERVER', 'waitress'):
        srv = WebDAVServer()
        # Avoid real signal handler registration in tests
        srv._setup_signal_handlers = lambda: None
        srv.start(port=8282, server_choice='auto')

    return captured.get('config', {})


def test_default_host_is_loopback_not_zero_zero():
    """Regression: `host` used to be hardcoded to "0.0.0.0". Must default
    to loopback when no host is configured."""
    cfg = _capture_wsgi_app(host_config_value=None)
    assert cfg.get('host') == '127.0.0.1', f"unexpected host: {cfg.get('host')!r}"


def test_explicit_host_is_respected():
    """If the user opts in to LAN binding via config, that must be honored."""
    cfg = _capture_wsgi_app(host_config_value='0.0.0.0')
    assert cfg.get('host') == '0.0.0.0'
