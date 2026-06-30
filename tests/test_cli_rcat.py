"""Hermetic tests for the `rcat` command (stdin -> Drive streaming upload).

No network: `auth_service` and `drive_service` are patched. We verify the CLI
plumbing — stdin is spooled to a temp file, the right filename/parent are
derived from REMOTE_PATH, conflict policy is forwarded, and edge cases
(empty stdin, folder-only path) fail cleanly.
"""
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from cli import cli


@pytest.fixture
def runner():
    return CliRunner()


def test_rcat_streams_stdin_and_derives_filename(runner):
    captured = {}

    def fake_upload(local_path, target_remote_parent_path_str,
                    target_folder_uuid, on_conflict, remote_filename):
        captured['bytes'] = Path(local_path).read_bytes()
        captured['filename'] = remote_filename
        captured['parent'] = target_remote_parent_path_str
        captured['conflict'] = on_conflict
        return 'uploaded'

    payload = b'\x00\x01\x02hello dump\xff' * 1000
    with patch('cli.auth_service.refresh_tokens'), \
         patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.resolve_path',
               return_value={'type': 'folder', 'uuid': 'puid', 'metadata': {}, 'path': '/backups'}), \
         patch('cli.drive_service.upload_single_item_with_conflict_handling',
               side_effect=fake_upload):
        result = runner.invoke(cli, ['rcat', '/backups/mydb.xz'], input=payload)

    assert result.exit_code == 0, result.output
    assert captured['bytes'] == payload          # exact stream spooled
    assert captured['filename'] == 'mydb.xz'     # filename parsed from path
    assert captured['parent'] == '/backups'      # parent dir parsed from path
    assert captured['conflict'] == 'overwrite'   # rcat default conflict policy
    assert '🎉' in result.output


def test_rcat_empty_stdin_fails(runner):
    with patch('cli.auth_service.refresh_tokens'), \
         patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.resolve_path',
               return_value={'type': 'folder', 'uuid': 'puid', 'metadata': {}, 'path': '/backups'}), \
         patch('cli.drive_service.upload_single_item_with_conflict_handling') as up:
        result = runner.invoke(cli, ['rcat', '/backups/empty.bin'], input=b'')
    assert result.exit_code == 1
    up.assert_not_called()
    assert 'empty' in result.output.lower()


def test_rcat_rejects_folder_path(runner):
    # Trailing slash => no filename; must fail before any network/auth call.
    with patch('cli.auth_service.refresh_tokens') as refresh:
        result = runner.invoke(cli, ['rcat', '/backups/'], input=b'data')
    assert result.exit_code == 1
    refresh.assert_not_called()
    assert 'filename' in result.output.lower()


def test_rcat_rejects_tty_stdin(runner):
    # Simulate an interactive terminal (no pipe) -> should refuse.
    with patch('sys.stdin') as fake_stdin:
        fake_stdin.isatty.return_value = True
        result = runner.invoke(cli, ['rcat', '/backups/x.bin'])
    assert result.exit_code == 1
    assert 'stdin' in result.output.lower()


def test_rcat_creates_missing_parent(runner):
    payload = b'streamed-bytes'
    calls = {'resolve': 0}

    def resolve(path):
        calls['resolve'] += 1
        # First call (existence check) -> missing; later call -> resolved path.
        if calls['resolve'] == 1:
            raise FileNotFoundError(path)
        return {'type': 'folder', 'uuid': 'new-parent', 'metadata': {}, 'path': '/new/dir'}

    with patch('cli.auth_service.refresh_tokens'), \
         patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.resolve_path', side_effect=resolve), \
         patch('cli.drive_service.create_folder_recursive',
               return_value={'uuid': 'new-parent'}) as mk, \
         patch('cli.drive_service.upload_single_item_with_conflict_handling',
               return_value='uploaded') as up:
        result = runner.invoke(cli, ['rcat', '/new/dir/file.txt'], input=payload)

    assert result.exit_code == 0, result.output
    mk.assert_called_once()
    up.assert_called_once()


def test_rcat_skip_conflict_reports_skipped(runner):
    with patch('cli.auth_service.refresh_tokens'), \
         patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.resolve_path',
               return_value={'type': 'folder', 'uuid': 'puid', 'metadata': {}, 'path': '/backups'}), \
         patch('cli.drive_service.upload_single_item_with_conflict_handling',
               return_value='skipped'):
        result = runner.invoke(
            cli, ['rcat', '/backups/exists.bin', '--on-conflict', 'skip'], input=b'data')
    assert result.exit_code == 0, result.output
    assert 'skipped' in result.output.lower()
