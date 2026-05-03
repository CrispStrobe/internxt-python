"""Tests for WebDAVServer.start() server-choice and SSL branches.

These cover the parts of start() that aren't a real listener — server
selection (auto/waitress/cheroot/invalid), SSL fallback for waitress,
SSL adapter setup for cheroot. The actual serve()/server.start() call is
patched to short-circuit.
"""
from unittest.mock import MagicMock, patch

import pytest

from services.webdav_server import WebDAVServer


@pytest.fixture
def server():
    s = WebDAVServer()
    s._setup_signal_handlers = lambda: None
    return s


def _start_with_mocks(server, *, server_choice='auto', protocol='http',
                       host='127.0.0.1', port=8282,
                       wsgi_server='waitress'):
    """Build a fully-mocked start() invocation. Returns (result_dict, captured)."""
    import sys
    from config.config import config_service
    from services.webdav_provider import InternxtDAVProvider

    captured = {'app_config': None, 'serve_kwargs': None,
                'cheroot_kwargs': None, 'cheroot_started': False}

    class FakeApp:
        def __init__(self, cfg):
            captured['app_config'] = cfg

    class FakeCherootServer:
        def __init__(self, **kw):
            captured['cheroot_kwargs'] = kw
            self.ssl_adapter = None
        def start(self):
            captured['cheroot_started'] = True
            raise RuntimeError("stop here")  # short-circuit

    fake_serve = MagicMock(side_effect=RuntimeError("stop here"))

    # Inject fake cheroot.wsgi module
    fake_cheroot_wsgi = MagicMock()
    fake_cheroot_wsgi.Server = FakeCherootServer

    fake_waitress = MagicMock()
    fake_waitress.serve = fake_serve

    saved_modules = {}
    for mod_name in ('cheroot.wsgi', 'waitress'):
        saved_modules[mod_name] = sys.modules.get(mod_name)
    sys.modules['cheroot.wsgi'] = fake_cheroot_wsgi
    sys.modules['waitress'] = fake_waitress

    cfg = {'host': host, 'port': port, 'protocol': protocol}
    try:
        with patch.object(config_service, 'read_webdav_config', return_value=cfg), \
             patch('wsgidav.wsgidav_app.WsgiDAVApp', FakeApp), \
             patch.object(InternxtDAVProvider, '__init__', return_value=None), \
             patch('services.webdav_server.WSGI_SERVER', wsgi_server):
            result = server.start(port=port, server_choice=server_choice)
    finally:
        for mod_name, prev in saved_modules.items():
            if prev is None:
                sys.modules.pop(mod_name, None)
            else:
                sys.modules[mod_name] = prev

    captured['serve_kwargs'] = fake_serve.call_args[1] if fake_serve.called else None
    return result, captured


# ---------- server choice routing ----------

def test_auto_falls_back_to_global_wsgi_server(server):
    """server_choice='auto' uses whatever WSGI_SERVER was detected at import."""
    result, captured = _start_with_mocks(server, server_choice='auto',
                                          wsgi_server='waitress')
    # Reached the waitress serve() call (then short-circuited via RuntimeError)
    assert captured['serve_kwargs'] is not None
    assert captured['serve_kwargs']['host'] == '127.0.0.1'


def test_explicit_waitress_choice_uses_waitress(server):
    result, captured = _start_with_mocks(server, server_choice='waitress',
                                          wsgi_server='cheroot')  # global says cheroot
    # We forced waitress, so waitress.serve must be the path taken
    assert captured['serve_kwargs'] is not None


def test_explicit_cheroot_choice_uses_cheroot(server):
    result, captured = _start_with_mocks(server, server_choice='cheroot',
                                          wsgi_server='waitress')
    # cheroot path → captured cheroot kwargs
    assert captured['cheroot_kwargs'] is not None
    assert captured['cheroot_started'] is True
    assert captured['cheroot_kwargs']['bind_addr'] == ('127.0.0.1', 8282)


def test_invalid_server_choice_returns_failure(server):
    """Anything other than auto/waitress/cheroot → ValueError caught,
    returned as success=False."""
    result, _ = _start_with_mocks(server, server_choice='nonexistent')
    assert result['success'] is False
    assert "Unknown server choice" in result['message']


# ---------- SSL branches ----------

def test_https_with_waitress_falls_back_to_http(server):
    """Waitress doesn't support SSL — must warn and fall back."""
    result, captured = _start_with_mocks(server, server_choice='waitress',
                                          protocol='https')
    # serve was called WITHOUT ssl_certificate (HTTP fallback)
    assert captured['serve_kwargs'] is not None
    assert 'ssl_certificate' not in captured['serve_kwargs']


def test_https_with_cheroot_attempts_ssl_setup(server):
    """Cheroot path must try to wire a BuiltinSSLAdapter."""
    import sys
    fake_adapter_class = MagicMock(return_value=MagicMock())
    fake_ssl_module = MagicMock()
    fake_ssl_module.BuiltinSSLAdapter = fake_adapter_class
    sys.modules['cheroot.ssl.builtin'] = fake_ssl_module

    try:
        with patch('services.network_utils.NetworkUtils.WEBDAV_SSL_CERT_FILE') as cert_path, \
             patch('services.network_utils.NetworkUtils.WEBDAV_SSL_KEY_FILE') as key_path:
            cert_path.exists.return_value = True
            key_path.exists.return_value = True
            result, captured = _start_with_mocks(server, server_choice='cheroot',
                                                  protocol='https')
    finally:
        sys.modules.pop('cheroot.ssl.builtin', None)

    # Cheroot started AND SSL adapter was constructed
    assert captured['cheroot_kwargs'] is not None
    fake_adapter_class.assert_called_once()


def test_https_with_cheroot_generates_certs_when_missing(server):
    """If cert/key files are missing on disk, SSL setup must generate them."""
    import sys
    fake_ssl_module = MagicMock()
    fake_ssl_module.BuiltinSSLAdapter = MagicMock(return_value=MagicMock())
    sys.modules['cheroot.ssl.builtin'] = fake_ssl_module

    try:
        with patch('services.network_utils.NetworkUtils.WEBDAV_SSL_CERT_FILE') as cert_path, \
             patch('services.network_utils.NetworkUtils.WEBDAV_SSL_KEY_FILE') as key_path, \
             patch('services.network_utils.NetworkUtils.generate_new_selfsigned_certs') as mock_gen:
            cert_path.exists.return_value = False
            key_path.exists.return_value = False
            _start_with_mocks(server, server_choice='cheroot', protocol='https')
    finally:
        sys.modules.pop('cheroot.ssl.builtin', None)

    mock_gen.assert_called_once()


# ---------- _create_wsgidav_app (the legacy helper, separate from start) ----------

def test_create_wsgidav_app_returns_app_instance(server):
    """Verifies the app factory builds a WsgiDAVApp without error."""
    captured_config = {}

    class FakeApp:
        def __init__(self, cfg):
            captured_config.update(cfg)

    from services.webdav_provider import InternxtDAVProvider
    with patch('services.webdav_server.WsgiDAVApp', FakeApp), \
         patch.object(InternxtDAVProvider, '__init__', return_value=None):
        result = server._create_wsgidav_app()
    # Returned a FakeApp instance
    assert isinstance(result, FakeApp)
    # Critical config keys present
    assert 'provider_mapping' in captured_config
    assert '/' in captured_config['provider_mapping']
    assert captured_config['simple_dc']['user_mapping']['*']['internxt']['password'] == 'internxt-webdav'
