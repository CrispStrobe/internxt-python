"""Tests for the `mv` Click command (batch move with wildcards & conflicts)."""
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from cli import cli


@pytest.fixture
def runner():
    return CliRunner()


def _resolve_factory(table):
    """Returns a fake resolve_path that uses the {path: info} table."""
    def fake(path):
        info = table.get(path)
        if info is None:
            raise FileNotFoundError(path)
        return info
    return fake


# ---------- argument validation ----------

def test_mv_requires_at_least_two_arguments(runner):
    result = runner.invoke(cli, ['mv', '/only-one-arg'])
    assert result.exit_code != 0
    assert 'at least one source' in result.output.lower() or 'source' in result.output.lower()


def test_mv_target_must_be_folder(runner):
    table = {
        '/src.txt': {'type': 'file', 'uuid': 'src', 'metadata': {}, 'path': '/src.txt'},
        '/target.txt': {'type': 'file', 'uuid': 'tgt', 'metadata': {}, 'path': '/target.txt'},
    }
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.resolve_path', side_effect=_resolve_factory(table)):
        result = runner.invoke(cli, ['mv', '/src.txt', '/target.txt'])
    assert result.exit_code == 1
    assert 'is a file, not a folder' in result.output


def test_mv_missing_target_returns_error(runner):
    table = {'/src.txt': {'type': 'file', 'uuid': 's', 'metadata': {}, 'path': '/src.txt'}}
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.resolve_path', side_effect=_resolve_factory(table)):
        result = runner.invoke(cli, ['mv', '/src.txt', '/missing'])
    assert result.exit_code == 1
    assert 'not found' in result.output.lower()


# ---------- happy path: single move ----------

def test_mv_single_file_into_folder(runner):
    table = {
        '/src.txt': {'type': 'file', 'uuid': 'src-uuid', 'metadata': {}, 'path': '/src.txt'},
        '/Archive': {'type': 'folder', 'uuid': 'arch-uuid', 'metadata': {}, 'path': '/Archive'},
    }
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.resolve_path', side_effect=_resolve_factory(table)), \
         patch('cli.drive_service.get_folder_content',
               return_value={'files': [], 'folders': []}), \
         patch('cli.drive_service.move_file', return_value={'ok': True}) as mock_move, \
         patch('cli.drive_service.move_folder') as mock_mvf:
        result = runner.invoke(cli, ['mv', '/src.txt', '/Archive', '-w', '1'])
    assert result.exit_code == 0, result.output
    mock_move.assert_called_once_with('src-uuid', 'arch-uuid')
    mock_mvf.assert_not_called()
    assert 'Moved' in result.output


def test_mv_folder_into_folder_uses_move_folder(runner):
    table = {
        '/Photos2024': {'type': 'folder', 'uuid': 'p24-uuid', 'metadata': {}, 'path': '/Photos2024'},
        '/Archive': {'type': 'folder', 'uuid': 'arch-uuid', 'metadata': {}, 'path': '/Archive'},
    }
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.resolve_path', side_effect=_resolve_factory(table)), \
         patch('cli.drive_service.get_folder_content',
               return_value={'files': [], 'folders': []}), \
         patch('cli.drive_service.move_folder', return_value={'ok': True}) as mock_mvf, \
         patch('cli.drive_service.move_file') as mock_mv:
        result = runner.invoke(cli, ['mv', '/Photos2024', '/Archive', '-w', '1'])
    assert result.exit_code == 0, result.output
    mock_mvf.assert_called_once_with('p24-uuid', 'arch-uuid')
    mock_mv.assert_not_called()


# ---------- wildcards / glob expansion ----------

def test_mv_glob_expands_to_multiple_sources(runner):
    """`mv "/Photos/*.jpg" /Archive` should expand against the parent listing."""
    table = {
        '/Archive': {'type': 'folder', 'uuid': 'arch', 'metadata': {}, 'path': '/Archive'},
    }
    listing = {
        'files': [
            {'display_name': 'a.jpg', 'plainName': 'a', 'type': 'jpg',
             'uuid': 'a-uuid', 'path': '/Photos/a.jpg'},
            {'display_name': 'b.jpg', 'plainName': 'b', 'type': 'jpg',
             'uuid': 'b-uuid', 'path': '/Photos/b.jpg'},
            {'display_name': 'note.txt', 'plainName': 'note', 'type': 'txt',
             'uuid': 'n-uuid', 'path': '/Photos/note.txt'},
        ],
        'folders': [],
    }

    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.resolve_path', side_effect=_resolve_factory(table)), \
         patch('cli.drive_service.list_folder_with_paths', return_value=listing), \
         patch('cli.drive_service.get_folder_content',
               return_value={'files': [], 'folders': []}), \
         patch('cli.drive_service.move_file') as mock_move:
        result = runner.invoke(cli, ['mv', '/Photos/*.jpg', '/Archive', '-w', '1'])

    assert result.exit_code == 0, result.output
    moved_uuids = {call.args[0] for call in mock_move.call_args_list}
    assert moved_uuids == {'a-uuid', 'b-uuid'}  # note.txt must NOT match


def test_mv_glob_with_no_matches_warns(runner):
    table = {
        '/Archive': {'type': 'folder', 'uuid': 'arch', 'metadata': {}, 'path': '/Archive'},
    }
    listing = {'files': [], 'folders': []}
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.resolve_path', side_effect=_resolve_factory(table)), \
         patch('cli.drive_service.list_folder_with_paths', return_value=listing), \
         patch('cli.drive_service.get_folder_content',
               return_value={'files': [], 'folders': []}):
        result = runner.invoke(cli, ['mv', '/Empty/*.jpg', '/Archive'])
    # When nothing matches, the command should exit non-zero with a clear message
    assert result.exit_code == 1
    assert 'Nothing to move' in result.output or 'Not found' in result.output


# ---------- conflict handling ----------

def test_mv_skip_when_target_already_has_same_name(runner):
    table = {
        '/src.txt': {'type': 'file', 'uuid': 'src-uuid', 'metadata': {}, 'path': '/src.txt'},
        '/Archive': {'type': 'folder', 'uuid': 'arch-uuid', 'metadata': {}, 'path': '/Archive'},
    }
    target_listing = {
        'files': [{'plainName': 'src', 'type': 'txt', 'uuid': 'existing-uuid'}],
        'folders': [],
    }
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.resolve_path', side_effect=_resolve_factory(table)), \
         patch('cli.drive_service.get_folder_content', return_value=target_listing), \
         patch('cli.drive_service.move_file') as mock_move:
        result = runner.invoke(cli, ['mv', '/src.txt', '/Archive', '-w', '1'])
    assert result.exit_code == 0, result.output
    mock_move.assert_not_called()
    # Summary mentions 1 skipped
    assert 'Skipped' in result.output


def test_mv_overwrite_deletes_existing_then_moves(runner):
    table = {
        '/src.txt': {'type': 'file', 'uuid': 'src-uuid', 'metadata': {}, 'path': '/src.txt'},
        '/Archive': {'type': 'folder', 'uuid': 'arch-uuid', 'metadata': {}, 'path': '/Archive'},
    }
    target_listing = {
        'files': [{'plainName': 'src', 'type': 'txt', 'uuid': 'existing-uuid'}],
        'folders': [],
    }
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.resolve_path', side_effect=_resolve_factory(table)), \
         patch('cli.drive_service.get_folder_content', return_value=target_listing), \
         patch('cli.drive_service.delete_permanently_file',
               return_value={'success': True}) as mock_del, \
         patch('cli.drive_service.move_file', return_value={'ok': True}) as mock_move:
        result = runner.invoke(cli, ['mv', '/src.txt', '/Archive',
                                      '--on-conflict', 'overwrite', '-w', '1'])
    assert result.exit_code == 0, result.output
    mock_del.assert_called_once_with('existing-uuid')
    mock_move.assert_called_once_with('src-uuid', 'arch-uuid')


def test_mv_overwrite_refuses_to_replace_folder_with_file(runner):
    table = {
        '/src.txt': {'type': 'file', 'uuid': 'src-uuid', 'metadata': {}, 'path': '/src.txt'},
        '/Archive': {'type': 'folder', 'uuid': 'arch-uuid', 'metadata': {}, 'path': '/Archive'},
    }
    # Target already contains a *folder* called 'src.txt' (weird but legal)
    target_listing = {
        'files': [],
        'folders': [{'plainName': 'src.txt', 'uuid': 'existing-folder-uuid'}],
    }
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.resolve_path', side_effect=_resolve_factory(table)), \
         patch('cli.drive_service.get_folder_content', return_value=target_listing), \
         patch('cli.drive_service.delete_permanently_file') as mock_del, \
         patch('cli.drive_service.move_file') as mock_move:
        result = runner.invoke(cli, ['mv', '/src.txt', '/Archive',
                                      '--on-conflict', 'overwrite', '-w', '1'])
    assert result.exit_code == 0, result.output
    assert 'Refusing to overwrite folder' in result.output
    mock_del.assert_not_called()
    mock_move.assert_not_called()


# ---------- dry-run ----------

def test_mv_dry_run_does_not_call_move(runner):
    table = {
        '/src.txt': {'type': 'file', 'uuid': 'src-uuid', 'metadata': {}, 'path': '/src.txt'},
        '/Archive': {'type': 'folder', 'uuid': 'arch-uuid', 'metadata': {}, 'path': '/Archive'},
    }
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.resolve_path', side_effect=_resolve_factory(table)), \
         patch('cli.drive_service.get_folder_content',
               return_value={'files': [], 'folders': []}), \
         patch('cli.drive_service.move_file') as mock_move, \
         patch('cli.drive_service.move_folder') as mock_mvf:
        result = runner.invoke(cli, ['mv', '/src.txt', '/Archive', '--dry-run'])
    assert result.exit_code == 0, result.output
    mock_move.assert_not_called()
    mock_mvf.assert_not_called()
    assert 'DRY RUN' in result.output


# ---------- already-in-target short-circuit ----------

def test_mv_skips_items_already_in_target(runner):
    """Moving /Archive/x.txt → /Archive is a no-op; do not call the API."""
    table = {
        '/Archive/x.txt': {'type': 'file', 'uuid': 'x-uuid', 'metadata': {}, 'path': '/Archive/x.txt'},
        '/Archive': {'type': 'folder', 'uuid': 'arch-uuid', 'metadata': {}, 'path': '/Archive'},
    }
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.resolve_path', side_effect=_resolve_factory(table)), \
         patch('cli.drive_service.get_folder_content',
               return_value={'files': [], 'folders': []}), \
         patch('cli.drive_service.move_file') as mock_move:
        result = runner.invoke(cli, ['mv', '/Archive/x.txt', '/Archive'])
    assert result.exit_code == 0, result.output
    mock_move.assert_not_called()
    assert 'already in the target folder' in result.output.lower()


# ---------- error propagation ----------

def test_mv_returns_nonzero_when_individual_move_fails(runner):
    table = {
        '/a.txt': {'type': 'file', 'uuid': 'a', 'metadata': {}, 'path': '/a.txt'},
        '/b.txt': {'type': 'file', 'uuid': 'b', 'metadata': {}, 'path': '/b.txt'},
        '/Archive': {'type': 'folder', 'uuid': 'arch', 'metadata': {}, 'path': '/Archive'},
    }

    call_count = {'n': 0}
    def fake_move(uuid, target):
        call_count['n'] += 1
        if uuid == 'b':
            raise RuntimeError("server hiccup")
        return {'ok': True}

    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.resolve_path', side_effect=_resolve_factory(table)), \
         patch('cli.drive_service.get_folder_content',
               return_value={'files': [], 'folders': []}), \
         patch('cli.drive_service.move_file', side_effect=fake_move):
        result = runner.invoke(cli, ['mv', '/a.txt', '/b.txt', '/Archive', '-w', '1'])
    assert result.exit_code == 1
    assert call_count['n'] == 2  # both attempted
    assert 'Errors' in result.output
