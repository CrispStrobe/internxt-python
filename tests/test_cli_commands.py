"""End-to-end CLI command tests via Click's CliRunner.

These don't touch the network — they patch `auth_service` and `drive_service`
to verify the CLI plumbing (argv parsing, exit codes, confirmation prompts,
error handling) is correct.
"""
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from cli import cli


@pytest.fixture
def runner():
    return CliRunner()


# ---------- whoami ----------

def test_whoami_logged_in(runner):
    fake = {'email': 'u@example.com', 'uuid': 'user-uuid', 'rootFolderId': 'root-uuid'}
    with patch('cli.auth_service.whoami', return_value=fake):
        result = runner.invoke(cli, ['whoami'])
    assert result.exit_code == 0, result.output
    assert 'u@example.com' in result.output
    assert 'user-uuid' in result.output
    assert 'root-uuid' in result.output


def test_whoami_logged_out(runner):
    with patch('cli.auth_service.whoami', return_value=None):
        result = runner.invoke(cli, ['whoami'])
    assert result.exit_code == 0, result.output
    assert 'Not logged in' in result.output


def test_whoami_handles_exception(runner):
    with patch('cli.auth_service.whoami', side_effect=RuntimeError("boom")):
        result = runner.invoke(cli, ['whoami'])
    # Command catches and prints to stderr; exit code 0 (no sys.exit).
    assert 'boom' in (result.output + (result.stderr_bytes or b'').decode())


# ---------- logout ----------

def test_logout_invokes_auth(runner):
    with patch('cli.auth_service.logout') as mock_logout:
        result = runner.invoke(cli, ['logout'])
    assert result.exit_code == 0
    mock_logout.assert_called_once()
    assert 'Successfully logged out' in result.output


# ---------- trash-path ----------

def test_trash_path_with_force_skips_confirm(runner):
    resolved = {'type': 'file', 'uuid': 'f1', 'metadata': {}, 'path': '/x.txt'}
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.resolve_path', return_value=resolved), \
         patch('cli.drive_service.trash_by_path',
               return_value={'message': 'File moved to trash'}) as mock_trash:
        result = runner.invoke(cli, ['trash-path', '/x.txt', '--force'])
    assert result.exit_code == 0, result.output
    mock_trash.assert_called_once_with('/x.txt')
    assert 'moved to trash' in result.output.lower() or 'trash' in result.output


def test_trash_path_prompts_without_force_and_aborts_on_no(runner):
    resolved = {'type': 'folder', 'uuid': 'f1', 'metadata': {}, 'path': '/f'}
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.resolve_path', return_value=resolved), \
         patch('cli.drive_service.trash_by_path') as mock_trash:
        # Answer "n" to the confirmation prompt.
        result = runner.invoke(cli, ['trash-path', '/f'], input='n\n')
    assert result.exit_code == 0, result.output
    mock_trash.assert_not_called()
    assert 'Cancelled' in result.output


def test_trash_path_missing_returns_error(runner):
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.resolve_path', side_effect=FileNotFoundError("no such")):
        result = runner.invoke(cli, ['trash-path', '/missing', '--force'])
    assert result.exit_code == 1


# ---------- delete-path (PERMANENT, must require confirmation) ----------

def test_delete_path_requires_confirmation_without_force(runner):
    resolved = {'type': 'file', 'uuid': 'x', 'metadata': {}, 'path': '/x'}
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.resolve_path', return_value=resolved), \
         patch('cli.drive_service.delete_permanently_by_path') as mock_del:
        result = runner.invoke(cli, ['delete-path', '/x'], input='n\n')
    assert result.exit_code == 0, result.output
    mock_del.assert_not_called()
    assert 'Cancelled' in result.output
    assert 'PERMANENTLY' in result.output  # the warning must appear


def test_delete_path_with_force_proceeds(runner):
    resolved = {'type': 'file', 'uuid': 'x', 'metadata': {}, 'path': '/x'}
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.resolve_path', return_value=resolved), \
         patch('cli.drive_service.delete_permanently_by_path',
               return_value={'message': 'Deleted'}) as mock_del:
        result = runner.invoke(cli, ['delete-path', '/x', '--force'])
    assert result.exit_code == 0, result.output
    mock_del.assert_called_once_with('/x')


def test_delete_path_yes_confirmation_proceeds(runner):
    resolved = {'type': 'folder', 'uuid': 'y', 'metadata': {}, 'path': '/y'}
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.resolve_path', return_value=resolved), \
         patch('cli.drive_service.delete_permanently_by_path',
               return_value={'message': 'Deleted'}) as mock_del:
        result = runner.invoke(cli, ['delete-path', '/y'], input='y\n')
    assert result.exit_code == 0, result.output
    mock_del.assert_called_once_with('/y')


# ---------- list-path ----------

def test_list_path_renders_folders_and_files(runner):
    content = {
        'current_path': '/Documents',
        'folders': [
            {'display_name': 'Reports', 'uuid': 'fold-uuid-1', 'path': '/Documents/Reports'},
        ],
        'files': [
            {'display_name': 'note.txt', 'uuid': 'file-uuid-1',
             'path': '/Documents/note.txt', 'size_display': '12 KB', 'size': 12000},
        ],
    }
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.list_folder_with_paths', return_value=content) as mock_list:
        result = runner.invoke(cli, ['list-path', '/Documents'])
    assert result.exit_code == 0, result.output
    mock_list.assert_called_once_with('/Documents')
    assert 'Reports' in result.output
    assert 'note.txt' in result.output


def test_list_path_default_root(runner):
    content = {'current_path': '/', 'folders': [], 'files': []}
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.list_folder_with_paths', return_value=content) as mock_list:
        result = runner.invoke(cli, ['list-path'])
    assert result.exit_code == 0, result.output
    mock_list.assert_called_once_with('/')


# ---------- mkdir ----------

def test_mkdir_creates_folder(runner):
    with patch('cli.auth_service.get_auth_details', return_value={'user': {'rootFolderId': 'r'}}), \
         patch('cli.drive_service.create_folder',
               return_value={'plainName': 'NewFolder', 'uuid': 'newuuid'}) as mock_create:
        result = runner.invoke(cli, ['mkdir', 'NewFolder'])
    assert result.exit_code == 0, result.output
    mock_create.assert_called_once()
    # The folder name should be the first positional arg.
    args, _ = mock_create.call_args
    assert args[0] == 'NewFolder'


# ---------- help discoverability ----------

def test_root_help_lists_path_commands(runner):
    result = runner.invoke(cli, ['--help'])
    assert result.exit_code == 0
    for cmd in ('list-path', 'download-path', 'trash-path', 'delete-path',
                'upload', 'whoami', 'login', 'logout'):
        assert cmd in result.output, f"missing command in --help: {cmd}"


def test_unknown_command_returns_nonzero(runner):
    result = runner.invoke(cli, ['this-command-does-not-exist'])
    assert result.exit_code != 0
