"""Tests for drive_service.create_folder (with cache-update + timestamps)
and drive_service.update_file (the WebDAV PUT-on-existing path).
"""
from unittest.mock import patch

import pytest

from services.drive import drive_service


@pytest.fixture(autouse=True)
def _reset():
    drive_service.folder_content_cache.clear()
    drive_service._mem_reserved = 0
    yield
    drive_service.folder_content_cache.clear()
    drive_service._mem_reserved = 0


@pytest.fixture
def fake_creds():
    return {
        'user': {
            'rootFolderId': 'root-uuid',
            'bucket': '00' * 12,
            'mnemonic': ('abandon abandon abandon abandon abandon abandon '
                         'abandon abandon abandon abandon abandon about'),
            'bridgeUser': 'u@example.com',
            'userId': 'u-42',
        },
    }


# ---------- create_folder ----------

def test_create_folder_uses_root_when_no_parent_given(fake_creds):
    """If parent_folder_uuid is None, must default to user's rootFolderId."""
    captured = {}
    def fake_api_create(payload):
        captured['payload'] = payload
        return {'uuid': 'new-uuid', 'plainName': payload['plainName']}

    with patch.object(drive_service.auth, 'get_auth_details',
                      return_value=fake_creds), \
         patch.object(drive_service.api, 'create_folder',
                      side_effect=fake_api_create):
        result = drive_service.create_folder('NewDir')

    assert captured['payload']['parentFolderUuid'] == 'root-uuid'
    assert captured['payload']['plainName'] == 'NewDir'
    assert result['uuid'] == 'new-uuid'


def test_create_folder_with_explicit_parent(fake_creds):
    captured = {}
    def fake_api_create(payload):
        captured['payload'] = payload
        return {'uuid': 'new-uuid'}
    with patch.object(drive_service.auth, 'get_auth_details',
                      return_value=fake_creds), \
         patch.object(drive_service.api, 'create_folder',
                      side_effect=fake_api_create):
        drive_service.create_folder('Sub', parent_folder_uuid='custom-parent')
    assert captured['payload']['parentFolderUuid'] == 'custom-parent'


def test_create_folder_includes_timestamps_when_provided(fake_creds):
    captured = {}
    def fake_api_create(payload):
        captured['payload'] = payload
        return {'uuid': 'new-uuid'}
    with patch.object(drive_service.auth, 'get_auth_details',
                      return_value=fake_creds), \
         patch.object(drive_service.api, 'create_folder',
                      side_effect=fake_api_create):
        drive_service.create_folder(
            'Dated', parent_folder_uuid='p',
            creation_time='2025-01-01T00:00:00Z',
            modification_time='2025-06-01T00:00:00Z',
        )
    assert captured['payload']['creationTime'] == '2025-01-01T00:00:00Z'
    assert captured['payload']['modificationTime'] == '2025-06-01T00:00:00Z'


def test_create_folder_omits_timestamps_when_none(fake_creds):
    captured = {}
    def fake_api_create(payload):
        captured['payload'] = payload
        return {'uuid': 'new-uuid'}
    with patch.object(drive_service.auth, 'get_auth_details',
                      return_value=fake_creds), \
         patch.object(drive_service.api, 'create_folder',
                      side_effect=fake_api_create):
        drive_service.create_folder('Plain', parent_folder_uuid='p')
    assert 'creationTime' not in captured['payload']
    assert 'modificationTime' not in captured['payload']


def test_create_folder_updates_parent_cache_when_present(fake_creds):
    """If parent is already cached, the new folder appears in subsequent listings
    without a re-fetch."""
    drive_service.folder_content_cache['parent-uuid'] = (
        9999999999.0, {'folders': [], 'files': []},
    )

    new_folder = {'uuid': 'new-uuid', 'plainName': 'NewDir'}
    with patch.object(drive_service.auth, 'get_auth_details',
                      return_value=fake_creds), \
         patch.object(drive_service.api, 'create_folder',
                      return_value=new_folder):
        drive_service.create_folder('NewDir', parent_folder_uuid='parent-uuid')

    _, content = drive_service.folder_content_cache['parent-uuid']
    assert any(f.get('uuid') == 'new-uuid' for f in content['folders'])


def test_create_folder_skips_cache_update_when_parent_not_cached(fake_creds):
    """If parent isn't cached, no cache mutation — the next get_folder_content
    call will fetch fresh."""
    new_folder = {'uuid': 'new-uuid', 'plainName': 'NewDir'}
    with patch.object(drive_service.auth, 'get_auth_details',
                      return_value=fake_creds), \
         patch.object(drive_service.api, 'create_folder',
                      return_value=new_folder):
        drive_service.create_folder('NewDir', parent_folder_uuid='unknown-parent')
    # Cache stays empty (no entry was created)
    assert 'unknown-parent' not in drive_service.folder_content_cache


def test_create_folder_raises_when_no_root_id():
    """If creds have no rootFolderId AND no explicit parent → ValueError."""
    bad_creds = {'user': {}}
    with patch.object(drive_service.auth, 'get_auth_details',
                      return_value=bad_creds):
        with pytest.raises(ValueError, match="No root folder"):
            drive_service.create_folder('X')


# ---------- update_file (WebDAV PUT-on-existing) ----------

def test_update_file_full_cycle(tmp_path, fake_creds):
    """update_file: read local file → encrypt → start upload → upload chunk →
    finish upload → replace_file metadata. Verify each step is called."""
    local = tmp_path / "doc.txt"
    local.write_bytes(b"updated content")

    with patch.object(drive_service.api, 'get_file_metadata',
                      return_value={'plainName': 'doc'}), \
         patch.object(drive_service.auth, 'get_auth_details',
                      return_value=fake_creds), \
         patch.object(drive_service.api, 'start_upload',
                      return_value={'uploads': [{
                          'index': 0, 'size': 100,
                          'url': 'https://upload', 'uuid': 'net-uuid',
                      }]}), \
         patch.object(drive_service.api, 'upload_chunk') as mock_chunk, \
         patch.object(drive_service.api, 'finish_upload',
                      return_value={'id': 'new-net-id'}), \
         patch.object(drive_service.api, 'replace_file',
                      return_value={'success': True}) as mock_replace, \
         patch.object(drive_service, '_clear_parent_cache_for_item'):
        result = drive_service.update_file('file-uuid', str(local))

    assert result['success'] is True
    mock_chunk.assert_called_once()
    # replace_file gets the new network file id and the local file size
    args, _ = mock_replace.call_args
    assert args[0] == 'file-uuid'
    assert args[1]['fileId'] == 'new-net-id'
    assert args[1]['size'] == len(b"updated content")


def test_update_file_clears_parent_cache(tmp_path, fake_creds):
    """After the update, parent cache must be invalidated so listings refresh."""
    local = tmp_path / "doc.txt"
    local.write_bytes(b"x")

    with patch.object(drive_service.api, 'get_file_metadata',
                      return_value={'plainName': 'doc'}), \
         patch.object(drive_service.auth, 'get_auth_details',
                      return_value=fake_creds), \
         patch.object(drive_service.api, 'start_upload',
                      return_value={'uploads': [{
                          'index': 0, 'size': 1,
                          'url': 'u', 'uuid': 'n',
                      }]}), \
         patch.object(drive_service.api, 'upload_chunk'), \
         patch.object(drive_service.api, 'finish_upload',
                      return_value={'id': 'nid'}), \
         patch.object(drive_service.api, 'replace_file',
                      return_value={}), \
         patch.object(drive_service, '_clear_parent_cache_for_item') as mock_clear:
        drive_service.update_file('file-uuid', str(local))

    mock_clear.assert_called_once_with('file-uuid', 'file')


def test_update_file_wraps_errors():
    with patch.object(drive_service.api, 'get_file_metadata',
                      side_effect=ConnectionError("net")):
        with pytest.raises(Exception, match="Failed to update file"):
            drive_service.update_file('file-uuid', '/tmp/nope')
