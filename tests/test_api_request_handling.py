"""Tests for ApiClient._make_request error mapping and robust_request 401 retry."""
import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from utils.api import ApiClient


@pytest.fixture
def client():
    """Fresh client per test so session state doesn't leak."""
    return ApiClient()


def _make_response(status=200, content=b'{}', json_body=None):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.content = content
    resp.text = content.decode() if isinstance(content, bytes) else str(content)
    if json_body is not None:
        resp.json.return_value = json_body
    else:
        try:
            resp.json.return_value = json.loads(content) if content else {}
        except (json.JSONDecodeError, TypeError):
            resp.json.side_effect = json.JSONDecodeError("err", "doc", 0)
    if 200 <= status < 400:
        resp.raise_for_status.return_value = None
    else:
        err = requests.exceptions.HTTPError(f"{status} error", response=resp)
        resp.raise_for_status.side_effect = err
    return resp


# ---------- _make_request happy path ----------

def test_make_request_returns_response_on_2xx(client):
    fake = _make_response(200, b'{"hello":"world"}')
    with patch.object(client.session, 'request', return_value=fake):
        out = client._make_request("GET", "https://api.example.com/x")
    assert out is fake


def test_make_request_strips_bearer_when_basic_auth_passed(client):
    """When auth=(user, pass) is passed, Authorization: Bearer must NOT also be sent."""
    client.session.headers['Authorization'] = 'Bearer some-token'
    captured = {}

    def fake_request(method, url, **kwargs):
        captured['headers'] = kwargs.get('headers', {})
        return _make_response(200, b'{}')

    with patch.object(client.session, 'request', side_effect=fake_request):
        client._make_request("POST", "https://api.example.com/x",
                             data={'k': 'v'}, auth=('u', 'p'))
    assert 'Authorization' not in captured['headers']


def test_make_request_passes_data_as_json_by_default(client):
    captured = {}
    def fake_request(method, url, **kwargs):
        captured['json'] = kwargs.get('json')
        captured['data'] = kwargs.get('data')
        return _make_response(200, b'{}')
    with patch.object(client.session, 'request', side_effect=fake_request):
        client._make_request("POST", "https://api.example.com/x", data={'k': 'v'})
    assert captured['json'] == {'k': 'v'}
    assert captured['data'] is None


def test_make_request_passes_data_as_body_when_is_json_false(client):
    """For pre-signed URL uploads, raw bytes go via data=, not json=."""
    captured = {}
    def fake_request(method, url, **kwargs):
        captured['json'] = kwargs.get('json')
        captured['data'] = kwargs.get('data')
        return _make_response(200, b'{}')
    with patch.object(client.session, 'request', side_effect=fake_request):
        client._make_request("PUT", "https://upload/", data=b'rawbytes', is_json=False)
    assert captured['json'] is None
    assert captured['data'] == b'rawbytes'


# ---------- _make_request error mapping ----------

def test_http_error_maps_to_value_error_with_message_from_body(client):
    fake = _make_response(404, b'{"message":"Not found"}',
                          json_body={'message': 'Not found'})
    with patch.object(client.session, 'request', return_value=fake):
        with pytest.raises(ValueError, match="Not found"):
            client._make_request("GET", "https://api.example.com/x")


def test_http_error_uses_default_when_body_is_not_json(client):
    fake = _make_response(500, b'<html>Server Error</html>')
    with patch.object(client.session, 'request', return_value=fake):
        with pytest.raises(ValueError, match="API Error"):
            client._make_request("GET", "https://api.example.com/x")


def test_network_error_maps_to_connection_error(client):
    with patch.object(client.session, 'request',
                      side_effect=requests.exceptions.ConnectionError("DNS failed")):
        with pytest.raises(ConnectionError, match="Network request failed"):
            client._make_request("GET", "https://api.example.com/x")


def test_timeout_maps_to_connection_error(client):
    with patch.object(client.session, 'request',
                      side_effect=requests.exceptions.Timeout("read timeout")):
        with pytest.raises(ConnectionError, match="Network request failed"):
            client._make_request("GET", "https://api.example.com/x")


# ---------- post / get / put / patch wrappers ----------

def test_post_returns_empty_dict_on_empty_body(client):
    fake = MagicMock(spec=requests.Response)
    fake.status_code = 204
    fake.content = b''
    fake.raise_for_status.return_value = None
    with patch.object(client, '_make_request', return_value=fake):
        out = client.post("https://x/y", data={'a': 1})
    assert out == {}


def test_get_returns_decoded_json(client):
    fake = MagicMock(spec=requests.Response)
    fake.content = b'{"k":"v"}'
    fake.json.return_value = {'k': 'v'}
    fake.raise_for_status.return_value = None
    with patch.object(client, '_make_request', return_value=fake):
        out = client.get("https://x/y")
    assert out == {'k': 'v'}


# ---------- robust_request: 401-rotate-and-retry ----------

def test_robust_request_retries_after_401_with_refreshed_token(client):
    """The headline 401 retry path: receive 401, refresh tokens, retry with
    fresh Bearer header, succeed."""
    # First call returns 401, second returns 200.
    first_resp = _make_response(401, b'{"message":"expired"}')
    second_resp = _make_response(200, b'{"ok":true}', json_body={'ok': True})

    request_calls = []
    def fake_request(method, url, **kwargs):
        request_calls.append({'auth_header': kwargs.get('headers', {}).get('Authorization')})
        return first_resp if len(request_calls) == 1 else second_resp

    fake_creds = {'token': 'fresh-token', 'newToken': 'fresh-new-token'}
    with patch.object(client.session, 'request', side_effect=fake_request), \
         patch('services.auth.auth_service.refresh_tokens') as mock_refresh, \
         patch('utils.api.config_service.read_user_credentials',
               return_value=fake_creds):
        result = client.robust_request("GET", "https://api.example.com/x")

    assert result == {'ok': True}
    mock_refresh.assert_called_once()
    # Two requests were made; the second carried the refreshed Bearer
    assert len(request_calls) == 2
    assert request_calls[1]['auth_header'] == 'Bearer fresh-token'


def test_robust_request_succeeds_on_first_try_no_refresh(client):
    fake = _make_response(200, b'{"k":"v"}', json_body={'k': 'v'})
    with patch.object(client.session, 'request', return_value=fake), \
         patch('services.auth.auth_service.refresh_tokens') as mock_refresh:
        result = client.robust_request("GET", "https://x/y")
    assert result == {'k': 'v'}
    mock_refresh.assert_not_called()


def test_robust_request_propagates_402_payment_required(client):
    """402 should bubble as ValueError (not silently swallowed) so the user
    sees subscription-status hints."""
    fake = _make_response(402, b'{"message":"Payment Required"}',
                          json_body={'message': 'Payment Required'})
    with patch.object(client.session, 'request', return_value=fake):
        with pytest.raises(ValueError, match="402"):
            client.robust_request("POST", "https://x/y")


def test_robust_request_propagates_409_conflict(client):
    """409 = item already exists; the upload code branches on this string."""
    fake = _make_response(409, b'{"message":"already exists"}',
                          json_body={'message': 'already exists'})
    with patch.object(client.session, 'request', return_value=fake):
        with pytest.raises(ValueError, match="409"):
            client.robust_request("POST", "https://x/y")
