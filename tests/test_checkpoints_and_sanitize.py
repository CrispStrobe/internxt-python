"""Tests for resumable-upload checkpoint helpers and filename sanitization."""
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from services.drive import drive_service


def _make_checkpoint(f: Path, bucket_id: str = 'bucket-1', file_size: int = None,
                     part_size: int = 100, parts: int = 3, **overrides):
    """A checkpoint dict shaped exactly like _upload_via_network writes."""
    data = {
        'version': 1,
        'file_path': str(f),
        'file_size': f.stat().st_size if file_size is None else file_size,
        'mtime_ns': f.stat().st_mtime_ns,
        'bucket_id': bucket_id,
        'part_size': part_size,
        'parts': parts,
        'index': 'ab' * 32,
        'uuid': 'net-uuid',
        'upload_id': 'UPID',
        'urls': [f'https://part/{i}' for i in range(parts)],
        'etags': {'1': 'etag-0'},
        'created': time.time(),
    }
    data.update(overrides)
    return data


# ---------- checkpoint save / load round-trip ----------

def test_checkpoint_roundtrip(tmp_path):
    f = tmp_path / "big.bin"
    f.write_bytes(b"x" * 300)
    with patch.object(drive_service.config, 'internxt_cli_data_dir', tmp_path):
        cp_path = drive_service._upload_checkpoint_path(f, 'bucket-1')
        data = _make_checkpoint(f)
        drive_service._save_upload_checkpoint(cp_path, data)
        assert cp_path.exists()
        loaded = drive_service._load_upload_checkpoint(f, 'bucket-1', 300, 100, 3)
    assert loaded == data


def test_checkpoint_path_is_deterministic_and_per_bucket(tmp_path):
    """Same (file, bucket) → same checkpoint path, so reruns find it;
    different bucket → different path."""
    f = tmp_path / "doc.txt"
    f.write_bytes(b"x")
    with patch.object(drive_service.config, 'internxt_cli_data_dir', tmp_path):
        a = drive_service._upload_checkpoint_path(f, 'bucket-A')
        b = drive_service._upload_checkpoint_path(f, 'bucket-A')
        c = drive_service._upload_checkpoint_path(f, 'bucket-B')
    assert a == b
    assert a != c


def test_checkpoint_save_is_atomic_no_tmp_left(tmp_path):
    f = tmp_path / "big.bin"
    f.write_bytes(b"x" * 300)
    with patch.object(drive_service.config, 'internxt_cli_data_dir', tmp_path):
        cp_path = drive_service._upload_checkpoint_path(f, 'bucket-1')
        drive_service._save_upload_checkpoint(cp_path, _make_checkpoint(f))
    assert not cp_path.with_suffix('.tmp').exists()


# ---------- checkpoint invalidation ----------

@pytest.mark.parametrize("mutate", [
    {'file_size': 999},          # file grew/shrank since the checkpoint
    {'parts': 4},                # different part layout
    {'part_size': 50},           # different part size
    {'created': 0},              # older than CHECKPOINT_MAX_AGE → URLs expired
    {'version': 2},              # unknown format
    {'urls': []},                # missing presigned URLs
    {'index': ''},               # missing crypto index
])
def test_stale_or_mismatched_checkpoint_is_rejected_and_deleted(tmp_path, mutate):
    f = tmp_path / "big.bin"
    f.write_bytes(b"x" * 300)
    with patch.object(drive_service.config, 'internxt_cli_data_dir', tmp_path):
        cp_path = drive_service._upload_checkpoint_path(f, 'bucket-1')
        drive_service._save_upload_checkpoint(cp_path, _make_checkpoint(f, **mutate))
        loaded = drive_service._load_upload_checkpoint(f, 'bucket-1', 300, 100, 3)
    assert loaded is None
    assert not cp_path.exists()


def test_checkpoint_rejected_when_file_modified(tmp_path):
    """mtime changed after the checkpoint → the recorded parts no longer match
    the file's bytes, so resume must not happen."""
    f = tmp_path / "big.bin"
    f.write_bytes(b"x" * 300)
    with patch.object(drive_service.config, 'internxt_cli_data_dir', tmp_path):
        cp_path = drive_service._upload_checkpoint_path(f, 'bucket-1')
        drive_service._save_upload_checkpoint(cp_path, _make_checkpoint(f))
        os.utime(f, ns=(f.stat().st_atime_ns, f.stat().st_mtime_ns + 10_000_000))
        loaded = drive_service._load_upload_checkpoint(f, 'bucket-1', 300, 100, 3)
    assert loaded is None


def test_load_missing_checkpoint_returns_none(tmp_path):
    f = tmp_path / "big.bin"
    f.write_bytes(b"x" * 300)
    with patch.object(drive_service.config, 'internxt_cli_data_dir', tmp_path):
        assert drive_service._load_upload_checkpoint(f, 'bucket-1', 300, 100, 3) is None


def test_load_corrupt_checkpoint_returns_none(tmp_path):
    f = tmp_path / "big.bin"
    f.write_bytes(b"x" * 300)
    with patch.object(drive_service.config, 'internxt_cli_data_dir', tmp_path):
        cp_path = drive_service._upload_checkpoint_path(f, 'bucket-1')
        cp_path.parent.mkdir(parents=True, exist_ok=True)
        cp_path.write_text('{not json')
        assert drive_service._load_upload_checkpoint(f, 'bucket-1', 300, 100, 3) is None


# ---------- remove / prune ----------

def test_remove_checkpoint_deletes_file(tmp_path):
    f = tmp_path / "big.bin"
    f.write_bytes(b"x" * 300)
    with patch.object(drive_service.config, 'internxt_cli_data_dir', tmp_path):
        cp_path = drive_service._upload_checkpoint_path(f, 'bucket-1')
        drive_service._save_upload_checkpoint(cp_path, _make_checkpoint(f))
        assert cp_path.exists()
        drive_service.remove_upload_checkpoint(str(cp_path))
    assert not cp_path.exists()


def test_remove_missing_checkpoint_is_noop():
    """Idempotent: removing a non-existent checkpoint must not raise."""
    drive_service.remove_upload_checkpoint('/tmp/nonexistent-checkpoint-xyz.json')


def test_prune_removes_only_expired_checkpoints(tmp_path):
    with patch.object(drive_service.config, 'internxt_cli_data_dir', tmp_path):
        cp_dir = tmp_path / 'upload_checkpoints'
        cp_dir.mkdir()
        old = cp_dir / 'old.json'
        fresh = cp_dir / 'fresh.json'
        old.write_text('{}')
        fresh.write_text('{}')
        expired = time.time() - drive_service.CHECKPOINT_MAX_AGE - 60
        os.utime(old, (expired, expired))
        drive_service._prune_upload_checkpoints()
    assert not old.exists()
    assert fresh.exists()


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
