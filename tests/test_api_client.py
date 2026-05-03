"""Tests for utils/api.py — focused on the delete-body bug fix."""
from unittest.mock import MagicMock, patch

from utils.api import ApiClient, api_client


def test_delete_now_accepts_data_body():
    """Regression: delete() used to ignore body, so delete_permanently silently no-op'd."""
    client = ApiClient()
    fake_resp = MagicMock()
    fake_resp.content = b''
    fake_resp.json.return_value = {}
    with patch.object(client, '_make_request', return_value=fake_resp) as mock_req:
        client.delete("https://x/y", data={"items": [{"uuid": "u1", "type": "file"}]},
                      headers={"Content-Type": "application/json"})
    # Verify data= was forwarded to _make_request
    mock_req.assert_called_once()
    _, kwargs = mock_req.call_args
    assert kwargs.get('data') == {"items": [{"uuid": "u1", "type": "file"}]}


def test_delete_permanently_sends_items_payload():
    """Regression for the data-loss bug: payload must reach the wire."""
    captured = {}

    def fake_make_request(method, url, **kwargs):
        captured['method'] = method
        captured['url'] = url
        captured['data'] = kwargs.get('data')
        resp = MagicMock()
        resp.content = b''
        resp.json.return_value = {}
        return resp

    with patch.object(api_client, '_make_request', side_effect=fake_make_request):
        api_client.delete_permanently("file-uuid-123", "file")

    assert captured['method'] == 'DELETE'
    assert captured['url'].endswith('/storage/trash')
    assert captured['data'] == {'items': [{'uuid': 'file-uuid-123', 'type': 'file'}]}


def test_move_file_endpoint_format():
    """Ensure move_file builds correct PATCH URL with destinationFolder field."""
    captured = {}

    def fake_make_request(method, url, **kwargs):
        captured['method'] = method
        captured['url'] = url
        captured['data'] = kwargs.get('data')
        resp = MagicMock()
        resp.content = b'{}'
        resp.json.return_value = {}
        return resp

    with patch.object(api_client, '_make_request', side_effect=fake_make_request):
        api_client.move_file("file-uuid-1", "folder-uuid-2")

    assert captured['method'] == 'PATCH'
    assert '/files/file-uuid-1' in captured['url']
    assert captured['data'] == {'destinationFolder': 'folder-uuid-2'}


def test_move_folder_endpoint_format():
    captured = {}

    def fake_make_request(method, url, **kwargs):
        captured['method'] = method
        captured['url'] = url
        captured['data'] = kwargs.get('data')
        resp = MagicMock()
        resp.content = b'{}'
        resp.json.return_value = {}
        return resp

    with patch.object(api_client, '_make_request', side_effect=fake_make_request):
        api_client.move_folder("folder-1", "folder-2")

    assert captured['method'] == 'PATCH'
    assert '/folders/folder-1' in captured['url']
    assert captured['data'] == {'destinationFolder': 'folder-2'}


def test_move_file_and_move_folder_defined_only_once():
    """Regression: there used to be two definitions of each (later one
    silently shadowing the earlier one)."""
    import utils.api as api_module
    src = open(api_module.__file__).read()
    assert src.count("    def move_file(") == 1
    assert src.count("    def move_folder(") == 1
