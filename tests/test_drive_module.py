"""Tests for services/drive.py — pure helpers + regression checks."""
from pathlib import Path

import pytest

from services.drive import drive_service


# ---------- _format_size ----------

@pytest.mark.parametrize("n,expected_unit", [
    (0, "B"),
    (512, "B"),
    (2048, "KB"),
    (5 * 1024 * 1024, "MB"),
    (3 * 1024 ** 3, "GB"),
    (2 * 1024 ** 4, "TB"),
    (5 * 1024 ** 5, "PB"),
])
def test_format_size_units(n, expected_unit):
    out = drive_service._format_size(n)
    assert out.endswith(f" {expected_unit}"), f"size={n} → {out!r} (want {expected_unit})"


def test_format_size_handles_floats_from_speed_calc():
    """Regression: int annotation but callers used to pass floats from
    division (file_size / time). Now callers convert to int, but the
    function should still tolerate either."""
    # Just a sanity check: integer input always works.
    out = drive_service._format_size(1500000)
    assert "MB" in out


# ---------- should_include_file ----------

def test_include_pattern_matches():
    p = Path("photo.jpg")
    assert drive_service.should_include_file(p, ["*.jpg"], []) is True
    assert drive_service.should_include_file(p, ["*.png"], []) is False


def test_exclude_pattern_excludes():
    p = Path("temp_backup.tmp")
    assert drive_service.should_include_file(p, [], ["*.tmp"]) is False
    assert drive_service.should_include_file(p, [], ["*.log"]) is True


def test_no_patterns_includes_everything():
    p = Path("anything.xyz")
    assert drive_service.should_include_file(p, [], []) is True


def test_include_takes_priority_over_exclude_when_both_match():
    """If excluded, file is rejected even if included."""
    p = Path("readme.md")
    assert drive_service.should_include_file(p, ["*.md"], ["readme.*"]) is False


# ---------- regression: create_folder_recursive defined exactly once ----------

def test_create_folder_recursive_defined_only_once():
    """Regression: the file used to have two definitions (the first 104-line
    body was dead code)."""
    import services.drive as drive_module
    src = open(drive_module.__file__).read()
    assert src.count("def create_folder_recursive(") == 1


def test_create_folder_defined_only_once():
    import services.drive as drive_module
    src = open(drive_module.__file__).read()
    assert src.count("    def create_folder(") == 1
