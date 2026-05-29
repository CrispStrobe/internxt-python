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


# ---------- trash-list ----------

def test_trash_list_empty(runner):
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.api_client.get_trash_content', return_value={'files': [], 'folders': []}):
        result = runner.invoke(cli, ['trash-list'])
    assert result.exit_code == 0, result.output
    assert 'empty' in result.output.lower()


def test_trash_list_shows_items(runner):
    items = {'files': [{'plainName': 'doc', 'type': 'pdf', 'size': '1024',
                         'uuid': 'f1', 'deletedAt': '2025-01-01'}],
             'folders': [{'plainName': 'Old', 'uuid': 'd1', 'updatedAt': '2025-02-01'}]}
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.api_client.get_trash_content', return_value=items):
        result = runner.invoke(cli, ['trash-list'])
    assert result.exit_code == 0, result.output
    assert 'doc.pdf' in result.output
    assert 'Old' in result.output
    assert 'f1' in result.output


def test_trash_list_json(runner):
    items = {'files': [{'plainName': 'a', 'type': 'txt', 'size': '10', 'uuid': 'u1'}],
             'folders': []}
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.api_client.get_trash_content', return_value=items):
        result = runner.invoke(cli, ['trash-list', '--json'])
    assert result.exit_code == 0, result.output
    import json
    parsed = json.loads(result.output)
    assert len(parsed) == 1


# ---------- trash-restore ----------

def test_trash_restore_file(runner):
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.api_client.restore_item', return_value={'success': True}):
        result = runner.invoke(cli, ['trash-restore', 'file-uuid-123'])
    assert result.exit_code == 0, result.output
    assert 'Restored' in result.output


def test_trash_restore_with_destination(runner):
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.resolve_path',
               return_value={'type': 'folder', 'uuid': 'dest-uuid'}), \
         patch('cli.api_client.restore_item', return_value={'success': True}) as mock_restore:
        result = runner.invoke(cli, ['trash-restore', 'item-uuid', '-d', '/Documents'])
    assert result.exit_code == 0, result.output
    mock_restore.assert_called_once_with('item-uuid', 'file', 'dest-uuid')


# ---------- trash-clear ----------

def test_trash_clear_with_force(runner):
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.api_client.clear_trash', return_value={}) as mock_clear:
        result = runner.invoke(cli, ['trash-clear', '--force'])
    assert result.exit_code == 0, result.output
    mock_clear.assert_called_once()
    assert 'cleared' in result.output.lower()


def test_trash_clear_aborts_without_confirm(runner):
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.api_client.clear_trash') as mock_clear:
        result = runner.invoke(cli, ['trash-clear'], input='n\n')
    assert result.exit_code == 0, result.output
    mock_clear.assert_not_called()
    assert 'Cancelled' in result.output


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


# ---------- quota ----------

def test_quota_shows_usage(runner):
    usage = {'drive': 1024 * 1024 * 500, 'backup': 0, 'total': 1024 * 1024 * 500}
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.api_client.get_storage_usage', return_value=usage):
        result = runner.invoke(cli, ['quota'])
    assert result.exit_code == 0, result.output
    assert 'Storage Usage' in result.output
    assert 'Drive' in result.output


def test_quota_json(runner):
    usage = {'drive': 100, 'backup': 0, 'total': 100}
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.api_client.get_storage_usage', return_value=usage):
        result = runner.invoke(cli, ['quota', '--json'])
    assert result.exit_code == 0, result.output
    import json
    parsed = json.loads(result.output)
    assert parsed['total'] == 100


def test_unknown_command_returns_nonzero(runner):
    result = runner.invoke(cli, ['this-command-does-not-exist'])
    assert result.exit_code != 0


# ---------- login with --tfa-secret (TOTP auto-generation) ----------

def test_login_tfa_secret_generates_code(runner):
    """--tfa-secret should auto-derive the 6-digit code via pyotp."""
    import pyotp
    secret = pyotp.random_base32()

    fake_creds = {
        'user': {'email': 'u@x.com', 'uuid': 'uid', 'rootFolderId': 'rf'},
        'token': 'tok',
    }
    with patch('cli.auth_service.is_2fa_needed', return_value=True), \
         patch('cli.auth_service.login', return_value=fake_creds) as mock_login:
        result = runner.invoke(cli, [
            'login', '--non-interactive',
            '--email', 'u@x.com', '--password', 'pw',
            '--tfa-secret', secret,
        ])
    assert result.exit_code == 0, result.output
    assert 'Generated 2FA code from TOTP secret' in result.output
    # The auto-generated code was passed to auth_service.login
    mock_login.assert_called_once()
    call_args = mock_login.call_args
    tfa_arg = call_args[0][2] if len(call_args[0]) > 2 else call_args[1].get('tfa_code')
    assert tfa_arg is not None
    assert len(tfa_arg) == 6
    assert tfa_arg.isdigit()


def test_login_tfa_secret_via_env_var(runner):
    """INTERNXT_TFA_SECRET env var should work the same as --tfa-secret."""
    import pyotp
    secret = pyotp.random_base32()

    fake_creds = {
        'user': {'email': 'u@x.com', 'uuid': 'uid', 'rootFolderId': 'rf'},
        'token': 'tok',
    }
    with patch('cli.auth_service.is_2fa_needed', return_value=True), \
         patch('cli.auth_service.login', return_value=fake_creds), \
         patch.dict('os.environ', {'INTERNXT_TFA_SECRET': secret}):
        result = runner.invoke(cli, [
            'login', '--non-interactive',
            '--email', 'u@x.com', '--password', 'pw',
        ])
    assert result.exit_code == 0, result.output
    assert 'Generated 2FA code from TOTP secret' in result.output


def test_login_explicit_tfa_takes_precedence_over_secret(runner):
    """If --tfa is provided alongside --tfa-secret, the explicit code wins."""
    import pyotp
    secret = pyotp.random_base32()

    fake_creds = {
        'user': {'email': 'u@x.com', 'uuid': 'uid', 'rootFolderId': 'rf'},
        'token': 'tok',
    }
    with patch('cli.auth_service.is_2fa_needed', return_value=True), \
         patch('cli.auth_service.login', return_value=fake_creds) as mock_login:
        result = runner.invoke(cli, [
            'login', '--non-interactive',
            '--email', 'u@x.com', '--password', 'pw',
            '--tfa', '123456', '--tfa-secret', secret,
        ])
    assert result.exit_code == 0, result.output
    # The explicit code was used, not the auto-generated one
    assert 'Generated 2FA code from TOTP secret' not in result.output
    call_args = mock_login.call_args
    tfa_arg = call_args[0][2] if len(call_args[0]) > 2 else call_args[1].get('tfa_code')
    assert tfa_arg == '123456'
