"""Tests for services/network_utils.py."""
from unittest.mock import patch


from services.network_utils import NetworkUtils


# ---------- parse_range_header ----------

def test_range_simple_byte_range():
    r = NetworkUtils.parse_range_header("bytes=0-99", file_size=1000)
    assert r == {'start': 0, 'end': 99, 'length': 100, 'total_size': 1000}


def test_range_open_ended():
    r = NetworkUtils.parse_range_header("bytes=500-", file_size=1000)
    assert r == {'start': 500, 'end': 999, 'length': 500, 'total_size': 1000}


def test_range_suffix():
    r = NetworkUtils.parse_range_header("bytes=-100", file_size=1000)
    assert r == {'start': 900, 'end': 999, 'length': 100, 'total_size': 1000}


def test_range_suffix_larger_than_file_clamps():
    r = NetworkUtils.parse_range_header("bytes=-5000", file_size=1000)
    assert r == {'start': 0, 'end': 999, 'length': 1000, 'total_size': 1000}


def test_range_invalid_returns_none():
    assert NetworkUtils.parse_range_header(None, 1000) is None
    assert NetworkUtils.parse_range_header("", 1000) is None
    assert NetworkUtils.parse_range_header("not-bytes-prefix", 1000) is None
    assert NetworkUtils.parse_range_header("bytes=abc", 1000) is None
    assert NetworkUtils.parse_range_header("bytes=", 1000) is None


def test_range_unsatisfiable_returns_none():
    assert NetworkUtils.parse_range_header("bytes=2000-3000", file_size=1000) is None
    # start > end
    assert NetworkUtils.parse_range_header("bytes=500-200", file_size=1000) is None


def test_range_multi_range_unsupported():
    assert NetworkUtils.parse_range_header("bytes=0-99,200-299", 1000) is None


# ---------- test_webdav_connection: verify=False only for loopback ----------

class _FakeResp:
    def __init__(self):
        self.status_code = 200
        self.headers = {'Allow': 'PROPFIND', 'DAV': '1, 2', 'Server': 'fake'}


def test_webdav_connection_disables_tls_verify_only_for_localhost():
    fake = _FakeResp()
    with patch('requests.options', return_value=fake) as mock_opt:
        NetworkUtils.test_webdav_connection("http://localhost:8080/", "u", "p")
    _, kwargs = mock_opt.call_args
    assert kwargs['verify'] is False


def test_webdav_connection_disables_tls_verify_only_for_127_0_0_1():
    fake = _FakeResp()
    with patch('requests.options', return_value=fake) as mock_opt:
        NetworkUtils.test_webdav_connection("https://127.0.0.1:8443/", "u", "p")
    _, kwargs = mock_opt.call_args
    assert kwargs['verify'] is False


def test_webdav_connection_keeps_tls_verify_for_remote_hosts():
    """Regression: verify=False used to be unconditional. Must be True for remote hosts."""
    fake = _FakeResp()
    with patch('requests.options', return_value=fake) as mock_opt:
        NetworkUtils.test_webdav_connection("https://example.com/", "u", "p")
    _, kwargs = mock_opt.call_args
    assert kwargs['verify'] is True


def test_webdav_connection_keeps_tls_verify_for_lan_address():
    fake = _FakeResp()
    with patch('requests.options', return_value=fake) as mock_opt:
        NetworkUtils.test_webdav_connection("https://192.168.1.10/", "u", "p")
    _, kwargs = mock_opt.call_args
    assert kwargs['verify'] is True
