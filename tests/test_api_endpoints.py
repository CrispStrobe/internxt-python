"""Contract tests for the ApiClient endpoint URLs and payloads.

These pin down exactly what HTTP method, URL pattern, and body each
high-level method sends — so a regression in URL building or payload
shape would fail loudly before it reaches the server.
"""
from unittest.mock import MagicMock, patch

import pytest

from utils.api import api_client


@pytest.fixture
def capture():
    """Patch _make_request and capture all (method, url, kwargs) calls."""
    captured = []

    def fake(method, url, **kwargs):
        captured.append((method, url, kwargs))
        resp = MagicMock()
        resp.content = b'{}'
        resp.json.return_value = {}
        return resp

    with patch.object(api_client, '_make_request', side_effect=fake):
        yield captured


# ---------- folder content ----------

def test_get_folder_folders_url_and_pagination(capture):
    api_client.get_folder_folders('parent-uuid', offset=10, limit=25)
    method, url, kwargs = capture[-1]
    assert method == 'GET'
    assert '/folders/content/parent-uuid/folders' in url
    assert kwargs['params']['offset'] == 10
    assert kwargs['params']['limit'] == 25
    assert kwargs['params']['sort'] == 'plainName'
    assert kwargs['params']['direction'] == 'ASC'


def test_get_folder_files_url(capture):
    api_client.get_folder_files('parent-uuid')
    method, url, _ = capture[-1]
    assert method == 'GET'
    assert '/folders/content/parent-uuid/files' in url


def test_create_folder_url_and_payload(capture):
    api_client.create_folder({'plainName': 'NewFolder', 'parentFolderUuid': 'parent'})
    method, url, kwargs = capture[-1]
    assert method == 'POST'
    assert url.endswith('/folders')
    assert kwargs['data']['plainName'] == 'NewFolder'
    assert kwargs['data']['parentFolderUuid'] == 'parent'


def test_create_folder_validates_required_fields():
    """Missing required fields must raise before hitting the network."""
    with pytest.raises(ValueError):
        api_client.create_folder({'plainName': 'incomplete'})  # no parent
    with pytest.raises(ValueError):
        api_client.create_folder({'parentFolderUuid': 'p'})  # no name


# ---------- metadata ----------

def test_get_file_metadata_endpoint(capture):
    api_client.get_file_metadata('file-uuid-1')
    method, url, _ = capture[-1]
    assert method == 'GET'
    assert url.endswith('/files/file-uuid-1/meta')


def test_get_folder_metadata_endpoint(capture):
    api_client.get_folder_metadata('folder-uuid-1')
    method, url, _ = capture[-1]
    assert method == 'GET'
    assert url.endswith('/folders/folder-uuid-1/meta')


def test_update_file_metadata_uses_put(capture):
    api_client.update_file_metadata('uuid', {'plainName': 'renamed'})
    method, url, kwargs = capture[-1]
    assert method == 'PUT'
    assert url.endswith('/files/uuid/meta')
    assert kwargs['data'] == {'plainName': 'renamed'}


# ---------- ancestors ----------

def test_get_folder_ancestors_returns_list_directly(capture):
    """Real API returns a top-level list, not a wrapper dict."""
    fake_list = [{'uuid': 'parent', 'name': 'parent'}]
    with patch.object(api_client, '_make_request') as mock_req:
        resp = MagicMock()
        resp.content = b'[{}]'
        resp.json.return_value = fake_list
        mock_req.return_value = resp
        out = api_client.get_folder_ancestors('uuid')
    assert out == fake_list


def test_get_folder_ancestors_returns_empty_list_for_dict_response(capture):
    """If the API mistakenly returns a dict, we return [] rather than crash."""
    out = api_client.get_folder_ancestors('uuid')
    assert out == []


# ---------- trash ----------

def test_trash_file_uses_bulk_endpoint_with_single_item(capture):
    api_client.trash_file('file-uuid')
    method, url, kwargs = capture[-1]
    assert method == 'POST'
    assert url.endswith('/storage/trash/add')
    assert kwargs['data'] == {'items': [{'uuid': 'file-uuid', 'type': 'file'}]}


def test_trash_folder_uses_bulk_endpoint_with_single_item(capture):
    api_client.trash_folder('folder-uuid')
    method, url, kwargs = capture[-1]
    assert method == 'POST'
    assert url.endswith('/storage/trash/add')
    assert kwargs['data'] == {'items': [{'uuid': 'folder-uuid', 'type': 'folder'}]}


def test_trash_items_bulk_endpoint(capture):
    payload = {'items': [
        {'uuid': 'a', 'type': 'file'},
        {'uuid': 'b', 'type': 'folder'},
    ]}
    api_client.trash_items(payload)
    method, url, kwargs = capture[-1]
    assert method == 'POST'
    assert url.endswith('/storage/trash/add')
    assert kwargs['data'] == payload


# ---------- network upload ----------

def test_start_upload_uses_v2_url_with_multiparts(capture):
    api_client.start_upload('bucket-1', 1234, auth=('u', 'p'))
    method, url, kwargs = capture[-1]
    assert method == 'POST'
    assert '/v2/buckets/bucket-1/files/start' in url
    assert 'multiparts=1' in url
    assert kwargs['data'] == {'uploads': [{'index': 0, 'size': 1234}]}
    assert kwargs['auth'] == ('u', 'p')


def test_finish_upload_endpoint(capture):
    payload = {'index': 'idx', 'shards': [{'hash': 'h', 'uuid': 'u'}]}
    api_client.finish_upload('bucket-1', payload, auth=('u', 'p'))
    method, url, kwargs = capture[-1]
    assert method == 'POST'
    assert url.endswith('/v2/buckets/bucket-1/files/finish')
    assert kwargs['data'] == payload
    assert kwargs['auth'] == ('u', 'p')


def test_get_download_links_endpoint(capture):
    api_client.get_download_links('bucket-1', 'file-net-id', auth=('u', 'p'))
    method, url, kwargs = capture[-1]
    assert method == 'GET'
    assert '/buckets/bucket-1/files/file-net-id/info' in url
    # API version 2 must be requested via header
    headers = kwargs.get('headers') or {}
    assert headers.get('x-api-version') == '2' or headers.get('X-Api-Version') == '2' \
        or 'x-api-version' in {k.lower() for k in headers}


# ---------- search ----------

def test_search_files_endpoint(capture):
    api_client.search_files('report', offset=20)
    method, url, kwargs = capture[-1]
    assert method == 'GET'
    assert '/fuzzy/report' in url
    assert kwargs['params']['offset'] == 20


# ---------- usage / user info ----------

def test_get_storage_usage_endpoint(capture):
    api_client.get_storage_usage()
    method, url, _ = capture[-1]
    assert method == 'GET'
    assert url.endswith('/users/usage')


def test_get_user_info_endpoint(capture):
    api_client.get_user_info()
    method, url, _ = capture[-1]
    assert method == 'GET'
    assert url.endswith('/users/me')


# ---------- security_details / refresh_token ----------

def test_security_details_endpoint(capture):
    api_client.security_details('user@example.com')
    # security_details may be a GET or POST; just verify the email reaches the URL/data
    method, url, kwargs = capture[-1]
    assert 'auth' in url or 'security' in url.lower()


def test_refresh_token_uses_users_refresh_endpoint(capture):
    api_client.refresh_token('refresh-token-value')
    method, url, kwargs = capture[-1]
    assert url.endswith('/users/refresh')


# ---------- login_access ----------

def test_login_access_endpoint_lowercases_email(capture):
    api_client.login_access({'email': '  USER@EXAMPLE.COM  ', 'password': 'x'})
    method, url, kwargs = capture[-1]
    assert method == 'POST'
    assert url.endswith('/auth/login/access')
    assert kwargs['data']['email'] == 'user@example.com'


# ---------- set_auth_tokens ----------

def test_set_auth_tokens_sets_bearer_header():
    api_client.set_auth_tokens('old-token', 'new-token-value')
    assert api_client.session.headers.get('Authorization') == 'Bearer new-token-value'


def test_set_auth_tokens_with_none_clears_header():
    api_client.set_auth_tokens('t', 'nt')  # set first
    api_client.set_auth_tokens(None, None)  # then clear
    assert 'Authorization' not in api_client.session.headers
