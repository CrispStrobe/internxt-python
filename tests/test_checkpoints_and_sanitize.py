"""Tests for upload-checkpoint helpers and filename sanitization."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from services.drive import drive_service


# ---------- create_upload_checkpoint ----------

def test_checkpoint_writes_json_with_required_fields(tmp_path):
    test_file = tmp_path / "doc.txt"
    test_file.write_bytes(b"hello world")
    with patch.object(drive_service.config, 'internxt_cli_data_dir', tmp_path):
        cp_path = drive_service.create_upload_checkpoint(test_file, "target-uuid-1")

    cp_file = Path(cp_path)
    assert cp_file.exists()
    data = json.loads(cp_file.read_text())
    assert data['file_path'] == str(test_file)
    assert data['target_uuid'] == 'target-uuid-1'
    assert data['file_size'] == 11
    assert data['status'] == 'started'
    assert isinstance(data['timestamp'], (int, float))


def test_checkpoint_id_is_deterministic(tmp_path):
    """Same (file_path, target_uuid) → same checkpoint id, so resumes work."""
    f = tmp_path / "doc.txt"
    f.write_bytes(b"x")
    with patch.object(drive_service.config, 'internxt_cli_data_dir', tmp_path):
        a = drive_service.create_upload_checkpoint(f, "target-uuid")
        b = drive_service.create_upload_checkpoint(f, "target-uuid")
    assert a == b


def test_checkpoint_id_differs_per_target(tmp_path):
    f = tmp_path / "doc.txt"
    f.write_bytes(b"x")
    with patch.object(drive_service.config, 'internxt_cli_data_dir', tmp_path):
        a = drive_service.create_upload_checkpoint(f, "target-A")
        b = drive_service.create_upload_checkpoint(f, "target-B")
    assert a != b


def test_remove_checkpoint_deletes_file(tmp_path):
    f = tmp_path / "doc.txt"
    f.write_bytes(b"x")
    with patch.object(drive_service.config, 'internxt_cli_data_dir', tmp_path):
        cp = drive_service.create_upload_checkpoint(f, "target-uuid")
    assert Path(cp).exists()
    drive_service.remove_upload_checkpoint(cp)
    assert not Path(cp).exists()


def test_remove_missing_checkpoint_is_noop():
    """Idempotent: removing a non-existent checkpoint must not raise."""
    drive_service.remove_upload_checkpoint('/tmp/nonexistent-checkpoint-xyz.json')


# ---------- sanitize_filename ----------

@pytest.mark.parametrize("dirty,clean_chars", [
    ('foo<bar', '<'),
    ('foo>bar', '>'),
    ('foo:bar', ':'),
    ('foo"bar', '"'),
    ('foo/bar', '/'),
    ('foo\\bar', '\\'),
    ('foo|bar', '|'),
    ('foo?bar', '?'),
    ('foo*bar', '*'),
])
def test_sanitize_replaces_each_invalid_char(dirty, clean_chars):
    out = drive_service.sanitize_filename(dirty)
    assert clean_chars not in out


def test_sanitize_strips_leading_trailing_dots_and_spaces():
    assert drive_service.sanitize_filename('  .file.  ') == 'file'


def test_sanitize_empty_returns_default():
    assert drive_service.sanitize_filename('') == 'unnamed_file'


def test_sanitize_only_invalid_chars_returns_default():
    """If sanitization strips everything, fall back to the default name."""
    out = drive_service.sanitize_filename('   ...   ')
    assert out == 'unnamed_file'


def test_sanitize_preserves_normal_filenames():
    for name in ('report.pdf', 'photo_2024.jpg', 'My Document.docx',
                 'data-2024-01.csv', 'file.with.many.dots.txt'):
        assert drive_service.sanitize_filename(name) == name


def test_sanitize_unicode_preserved():
    assert drive_service.sanitize_filename('résumé.pdf') == 'résumé.pdf'
    assert drive_service.sanitize_filename('文档.txt') == '文档.txt'
