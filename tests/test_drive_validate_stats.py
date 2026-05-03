"""Tests for drive_service validation/statistics helpers:
validate_upload_sources, get_upload_statistics.
"""
from pathlib import Path


from services.drive import drive_service


# ---------- validate_upload_sources ----------

def test_validate_returns_valid_paths_for_real_files(tmp_path):
    a = tmp_path / "a.txt"
    a.write_bytes(b"hello")
    b = tmp_path / "b.txt"
    b.write_bytes(b"world")
    valid, errors = drive_service.validate_upload_sources([str(a), str(b)])
    assert len(valid) == 2
    assert errors == []


def test_validate_reports_missing_source(tmp_path):
    valid, errors = drive_service.validate_upload_sources([str(tmp_path / "ghost.txt")])
    assert valid == []
    assert any("not found" in e.lower() for e in errors)


def test_validate_rejects_directory_without_recursive(tmp_path):
    sub = tmp_path / "subdir"
    sub.mkdir()
    valid, errors = drive_service.validate_upload_sources([str(sub)], recursive=False)
    assert valid == []
    assert any("recursive" in e.lower() for e in errors)


def test_validate_accepts_directory_with_recursive(tmp_path):
    sub = tmp_path / "subdir"
    sub.mkdir()
    valid, errors = drive_service.validate_upload_sources([str(sub)], recursive=True)
    assert len(valid) == 1
    assert errors == []


def test_validate_skips_oversized_files(tmp_path, monkeypatch):
    """Files over the 20 GB limit are reported as errors, not in valid list."""
    big = tmp_path / "big.bin"
    big.write_bytes(b"x")  # actually small

    # Patch stat to claim huge size
    real_stat = Path.stat
    def fake_stat(self, *a, **k):
        result = real_stat(self, *a, **k)
        if self == big:
            # Return a stat result with the file's existing fields but huge size.
            class Fake:
                def __init__(self, real, size):
                    for f in dir(real):
                        if f.startswith('st_'):
                            setattr(self, f, getattr(real, f))
                    self.st_size = size
            return Fake(result, drive_service.TWENTY_GIGABYTES + 1)
        return result
    monkeypatch.setattr(Path, 'stat', fake_stat)

    valid, errors = drive_service.validate_upload_sources([str(big)])
    assert valid == []
    assert any("too large" in e.lower() for e in errors)


def test_validate_mixes_valid_and_invalid(tmp_path):
    good = tmp_path / "good.txt"
    good.write_bytes(b"x")
    valid, errors = drive_service.validate_upload_sources(
        [str(good), str(tmp_path / "missing.txt")])
    assert len(valid) == 1
    assert len(errors) == 1


# ---------- get_upload_statistics ----------

def test_stats_for_single_file(tmp_path):
    f = tmp_path / "doc.txt"
    f.write_bytes(b"hello world")
    stats = drive_service.get_upload_statistics(f)
    assert stats['total_files'] == 1
    assert stats['total_size'] == 11
    assert stats['total_dirs'] == 0
    assert stats['file_list'] == [f]


def test_stats_for_directory_recursive(tmp_path):
    """Recursive walk counts files at every depth + intermediate dirs."""
    (tmp_path / "a.txt").write_bytes(b"a" * 100)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.txt").write_bytes(b"b" * 200)
    deeper = sub / "deeper"
    deeper.mkdir()
    (deeper / "c.txt").write_bytes(b"c" * 300)

    stats = drive_service.get_upload_statistics(tmp_path, recursive=True)
    assert stats['total_files'] == 3
    assert stats['total_size'] == 600
    assert stats['total_dirs'] == 2  # 'sub' and 'deeper'


def test_stats_for_directory_non_recursive_only_top_level(tmp_path):
    """Without recursive, only direct children are counted."""
    (tmp_path / "top.txt").write_bytes(b"t" * 50)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "deep.txt").write_bytes(b"d" * 100)

    stats = drive_service.get_upload_statistics(tmp_path, recursive=False)
    assert stats['total_files'] == 1  # 'top.txt' only
    assert stats['total_dirs'] == 1
    assert stats['total_size'] == 50


def test_stats_for_empty_directory(tmp_path):
    sub = tmp_path / "empty"
    sub.mkdir()
    stats = drive_service.get_upload_statistics(sub, recursive=True)
    assert stats['total_files'] == 0
    assert stats['total_size'] == 0
    assert stats['total_dirs'] == 0
    assert stats['file_list'] == []


def test_stats_for_nonexistent_path_is_empty(tmp_path):
    """Non-file, non-dir input yields empty stats (no crash)."""
    stats = drive_service.get_upload_statistics(tmp_path / "missing")
    assert stats['total_files'] == 0
    assert stats['total_size'] == 0
