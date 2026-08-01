"""Contract tests for the path-based copy command."""
from unittest.mock import patch

from click.testing import CliRunner

from cli import cli


def _resolver(table):
    def resolve(path):
        if path not in table:
            raise FileNotFoundError(path)
        return table[path]
    return resolve


def test_cp_file_into_folder():
    table = {
        '/source.txt': {'type': 'file', 'uuid': 'file-1'},
        '/Archive': {'type': 'folder', 'uuid': 'folder-1'},
    }
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.resolve_path', side_effect=_resolver(table)), \
         patch('cli.drive_service.copy_item', return_value={'success': True}) as copy:
        result = CliRunner().invoke(cli, ['cp', '/source.txt', '/Archive'])
    assert result.exit_code == 0, result.output
    copy.assert_called_once_with('file-1', 'folder-1')


def test_cp_folder_into_folder():
    table = {
        '/Photos': {'type': 'folder', 'uuid': 'photos-1'},
        '/Archive': {'type': 'folder', 'uuid': 'archive-1'},
    }
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.resolve_path', side_effect=_resolver(table)), \
         patch('cli.drive_service.copy_folder', return_value={'success': True}) as copy:
        result = CliRunner().invoke(cli, ['cp', '/Photos', '/Archive'])
    assert result.exit_code == 0, result.output
    copy.assert_called_once_with('photos-1', 'archive-1')


def test_cp_rejects_file_target():
    table = {
        '/source.txt': {'type': 'file', 'uuid': 'file-1'},
        '/target.txt': {'type': 'file', 'uuid': 'target-1'},
    }
    with patch('cli.auth_service.get_auth_details', return_value={}), \
         patch('cli.drive_service.resolve_path', side_effect=_resolver(table)):
        result = CliRunner().invoke(cli, ['cp', '/source.txt', '/target.txt'])
    assert result.exit_code != 0
    assert 'not a folder' in result.output
