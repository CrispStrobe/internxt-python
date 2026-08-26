"""More CLI command tests via Click's CliRunner.

Covers: search, find, resolve, tree, download-path, upload (validation +
target-folder lookup branches).
"""
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from cli import cli


@pytest.fixture
def runner():
    return CliRunner()


# ---------- search ----------

def test_search_no_results_prints_helpful_message(runner):
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.search_drive', return_value=[]):
        result = runner.invoke(cli, ['search', 'no-such-thing'])
    assert result.exit_code == 0, result.output
    assert 'No items found' in result.output


def test_search_lists_files_and_folders(runner):
    fake_results = [
        {'name': 'Reports', 'plainName': 'Reports', 'itemId': 'fold-uuid',
         'itemType': 'folder', 'uuid': 'fold-uuid'},
        {'name': 'report.pdf', 'plainName': 'report', 'type': 'pdf',
         'itemId': 'file-uuid', 'itemType': 'file', 'uuid': 'file-uuid'},
    ]
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.search_drive', return_value=fake_results):
        result = runner.invoke(cli, ['search', 'report'])
    assert result.exit_code == 0, result.output
    assert 'Reports' in result.output
    assert 'report.pdf' in result.output


def test_search_detailed_fetches_metadata(runner):
    fake_results = [
        {'itemId': 'file-uuid', 'itemType': 'file', 'name': 'doc.txt'},
    ]
    detailed_meta = {
        'uuid': 'file-uuid', 'plainName': 'doc', 'type': 'txt',
        'size': 1234, 'updatedAt': '2024-01-01T00:00:00Z',
    }
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.search_drive', return_value=fake_results), \
         patch('cli.drive_service.get_file_metadata',
               return_value=detailed_meta) as mock_meta, \
         patch('cli.drive_service.get_full_path_for_item',
               return_value='/Documents/doc.txt'):
        result = runner.invoke(cli, ['search', 'doc', '--detailed'])
    assert result.exit_code == 0, result.output
    mock_meta.assert_called_once_with('file-uuid')
    assert '/Documents/doc.txt' in result.output


# ---------- find ----------

def test_find_requires_a_pattern(runner):
    result = runner.invoke(cli, ['find', '/'])
    assert result.exit_code == 1
    assert 'must provide' in result.output.lower() or 'name' in result.output.lower()


def test_find_rejects_both_name_and_iname(runner):
    result = runner.invoke(cli, ['find', '/', '-name', '*.py', '-iname', '*.PY'])
    assert result.exit_code == 1
    assert 'only use -name or -iname' in result.output.lower() or 'not both' in result.output.lower()


def test_find_with_name_pattern_calls_drive_service(runner):
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.find_files', return_value=[]) as mock_find:
        result = runner.invoke(cli, ['find', '/Photos', '-name', '*.jpg'])
    assert result.exit_code == 0, result.output
    mock_find.assert_called_once()
    _, kwargs = mock_find.call_args
    assert kwargs.get('case_sensitive') is True


def test_find_with_iname_pattern_is_case_insensitive(runner):
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.find_files', return_value=[]) as mock_find:
        result = runner.invoke(cli, ['find', '/', '-iname', '*.JPG'])
    assert result.exit_code == 0, result.output
    _, kwargs = mock_find.call_args
    assert kwargs.get('case_sensitive') is False


def test_find_with_results_renders_paths(runner):
    fake_results = [
        {'path': '/Photos/2024/img.jpg', 'uuid': 'photo-uuid' * 2,
         'size_display': '2.3 MB', 'modified': '2024-06-15T12:00:00'},
    ]
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.find_files', return_value=fake_results):
        result = runner.invoke(cli, ['find', '/Photos', '-name', '*.jpg'])
    assert result.exit_code == 0, result.output
    assert '/Photos/2024/img.jpg' in result.output
    assert '2.3 MB' in result.output


def test_find_no_results_prints_message(runner):
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.find_files', return_value=[]):
        result = runner.invoke(cli, ['find', '/', '-name', '*.nonexistent'])
    assert result.exit_code == 0, result.output
    assert 'No files found' in result.output


# ---------- resolve ----------

def test_resolve_for_file_shows_metadata(runner):
    resolved = {
        'type': 'file', 'uuid': 'file-uuid',
        'metadata': {'plainName': 'doc', 'type': 'pdf', 'size': 1024},
        'path': '/Documents/doc.pdf',
    }
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.resolve_path', return_value=resolved):
        result = runner.invoke(cli, ['resolve', '/Documents/doc.pdf'])
    assert result.exit_code == 0, result.output
    assert 'FILE' in result.output
    assert 'file-uuid' in result.output
    assert '/Documents/doc.pdf' in result.output


def test_resolve_for_folder(runner):
    resolved = {
        'type': 'folder', 'uuid': 'fold-uuid',
        'metadata': {'plainName': 'Documents'},
        'path': '/Documents',
    }
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.resolve_path', return_value=resolved):
        result = runner.invoke(cli, ['resolve', '/Documents'])
    assert result.exit_code == 0, result.output
    assert 'FOLDER' in result.output


def test_resolve_missing_path_returns_error(runner):
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.resolve_path',
               side_effect=FileNotFoundError("no such")):
        result = runner.invoke(cli, ['resolve', '/missing'])
    assert result.exit_code == 1
    assert 'not found' in result.output.lower()


# ---------- tree ----------

def test_tree_renders_folder_structure(runner):
    contents = {
        '/': {
            'current_path': '/',
            'folders': [
                {'display_name': 'Docs', 'path': '/Docs',
                 'plainName': 'Docs', 'uuid': 'docs-uuid'},
            ],
            'files': [],
        },
        '/Docs': {
            'current_path': '/Docs',
            'folders': [],
            'files': [
                {'display_name': 'a.txt', 'plainName': 'a', 'type': 'txt',
                 'uuid': 'a', 'size_display': '12 B'},
            ],
        },
    }

    def fake_list(path):
        return contents.get(path, {'folders': [], 'files': []})

    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.list_folder_with_paths', side_effect=fake_list):
        result = runner.invoke(cli, ['tree', '/', '--depth', '2'])
    assert result.exit_code == 0, result.output
    assert 'Docs' in result.output
    assert 'a.txt' in result.output


def test_tree_default_depth_is_3(runner):
    """Non-regression: default depth flag is documented as 3."""
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.list_folder_with_paths',
               return_value={'folders': [], 'files': []}):
        result = runner.invoke(cli, ['tree'])
    assert result.exit_code == 0
    assert '3 levels' in result.output


# ---------- upload (validation + target lookup) ----------

def test_upload_no_sources_returns_error(runner):
    result = runner.invoke(cli, ['upload'])
    assert result.exit_code == 1
    assert 'No source' in result.output


def test_upload_creates_missing_target_folder(runner):
    """If the target path doesn't exist, upload must auto-create it."""
    fake_target = {'uuid': 'created-uuid', 'plainName': 'NewFolder'}
    with patch('cli.auth_service.refresh_tokens'), \
         patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.resolve_path',
               side_effect=[FileNotFoundError("no"),
                            {'type': 'folder', 'uuid': 'created-uuid', 'path': '/NewFolder'}]), \
         patch('cli.drive_service.create_folder_recursive',
               return_value=fake_target) as mock_create, \
         patch('cli.drive_service.upload_single_item_with_conflict_handling',
               return_value='uploaded'), \
         patch('pathlib.Path.is_file', return_value=False), \
         patch('pathlib.Path.is_dir', return_value=False), \
         patch('pathlib.Path.exists', return_value=False):
        runner.invoke(cli, ['upload', '/local/missing.txt', '-t', '/NewFolder'])
    # Even though source doesn't exist, target creation path was exercised.
    mock_create.assert_called_once()


def test_upload_rejects_when_target_path_is_file(runner):
    """Cannot upload into a file."""
    target_info = {'type': 'file', 'uuid': 'f-uuid', 'path': '/some.file'}
    with patch('cli.auth_service.refresh_tokens'), \
         patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.resolve_path', return_value=target_info):
        result = runner.invoke(cli, ['upload', 'localfile', '-t', '/some.file'])
    assert result.exit_code == 1
    assert 'not a folder' in result.output


def test_upload_accepts_path_alias_for_target(runner, tmp_path):
    local_file = tmp_path / 'doc.txt'
    local_file.write_text('hello', encoding='utf-8')
    target_info = {'type': 'folder', 'uuid': 'target-uuid', 'path': '/Docs'}
    with patch('cli.auth_service.refresh_tokens'), \
         patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.resolve_path', return_value=target_info) as mock_resolve, \
         patch('cli.drive_service.upload_single_item_with_conflict_handling',
               return_value='uploaded') as mock_upload:
        result = runner.invoke(cli, ['upload', str(local_file), '--path', '/Docs'])
    assert result.exit_code == 0, result.output
    mock_resolve.assert_any_call('/Docs')
    mock_upload.assert_called_once()


def test_upload_accepts_legacy_positional_remote_target(runner, tmp_path):
    local_file = tmp_path / 'doc.txt'
    local_file.write_text('hello', encoding='utf-8')
    target_info = {'type': 'folder', 'uuid': 'target-uuid', 'path': '/Katharina'}
    with patch('cli.auth_service.refresh_tokens'), \
         patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.resolve_path', return_value=target_info) as mock_resolve, \
         patch('cli.drive_service.upload_single_item_with_conflict_handling',
               return_value='uploaded') as mock_upload:
        result = runner.invoke(cli, ['upload', str(local_file), '\\Katharina\\'])
    assert result.exit_code == 0, result.output
    assert 'Treating final argument as remote target path: /Katharina' in result.output
    mock_resolve.assert_any_call('/Katharina')
    mock_upload.assert_called_once()


def test_upload_infers_remote_target_even_with_preserve_timestamps_flag(runner, tmp_path):
    local_file = tmp_path / 'doc.txt'
    local_file.write_text('hello', encoding='utf-8')
    target_info = {'type': 'folder', 'uuid': 'target-uuid', 'path': '/Katharina'}
    with patch('cli.auth_service.refresh_tokens'), \
         patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.resolve_path', return_value=target_info) as mock_resolve, \
         patch('cli.drive_service.upload_single_item_with_conflict_handling',
               return_value='uploaded') as mock_upload:
        result = runner.invoke(
            cli,
            ['upload', str(local_file), '-p', '\\Katharina\\'],
        )
    assert result.exit_code == 0, result.output
    assert 'Treating final argument as remote target path: /Katharina' in result.output
    mock_resolve.assert_any_call('/Katharina')
    _, kwargs = mock_upload.call_args
    assert kwargs['modification_time'] is not None


# ---------- download-path ----------

def test_download_path_folder_without_recursive_errors(runner):
    folder_info = {'type': 'folder', 'uuid': 'fold-uuid', 'metadata': {}, 'path': '/Photos'}
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.resolve_path', return_value=folder_info):
        result = runner.invoke(cli, ['download-path', '/Photos'])
    assert result.exit_code == 1
    assert 'is a folder' in result.output
    assert '-r' in result.output


def test_download_path_missing_remote_returns_error(runner):
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.resolve_path',
               side_effect=FileNotFoundError("missing")):
        result = runner.invoke(cli, ['download-path', '/no-such-file.txt'])
    assert result.exit_code == 1


def test_download_path_filter_skips_filtered_file(runner, tmp_path):
    """If the file is excluded by --include patterns, exit cleanly with no download call."""
    # Note: resolve_path returns {'type': 'file', ...} where 'type' discriminates
    # file vs folder. The plainName lives at the top level here too (the cmd
    # reads item_info.get('plainName')).
    file_info = {
        'type': 'file', 'uuid': 'f-uuid',
        'metadata': {'plainName': 'doc'},
        'plainName': 'doc',
        'path': '/Docs/doc.txt',
    }
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.resolve_path', return_value=file_info), \
         patch('cli.drive_service.should_include_file', return_value=False), \
         patch('cli.drive_service.download_file') as mock_dl:
        result = runner.invoke(cli, ['download-path', '/Docs/doc.txt',
                                      '--include', '*.pdf',
                                      '-d', str(tmp_path / 'out.txt')])
    assert result.exit_code == 0, result.output
    mock_dl.assert_not_called()
    assert 'filtered out' in result.output.lower() or 'Filter' in result.output
