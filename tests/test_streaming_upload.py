"""Tests for the WebDAV StreamingFileUpload memory/disk hybrid buffer."""
import os

import pytest

from services.webdav_provider import StreamingFileUpload, MAX_MEMORY_SIZE


def test_small_file_stays_in_memory():
    buf = StreamingFileUpload(expected_size=100)
    buf.write(b"hello world")
    try:
        assert buf.using_disk is False
        assert buf.bytes_written == 11
        assert buf.get_data() == b"hello world"
    finally:
        buf.cleanup()


def test_large_expected_size_goes_to_disk_immediately():
    buf = StreamingFileUpload(expected_size=MAX_MEMORY_SIZE + 1)
    try:
        assert buf.using_disk is True
        # Tempfile must exist
        assert buf.temp_path and os.path.exists(buf.temp_path)
    finally:
        buf.cleanup()


def test_buffer_promotes_to_disk_when_writes_exceed_threshold():
    """Crossing MAX_MEMORY_SIZE mid-upload must transparently switch to disk."""
    buf = StreamingFileUpload()  # no expected size hint
    chunk = b"x" * (1024 * 1024)  # 1 MB chunks
    threshold_chunks = (MAX_MEMORY_SIZE // len(chunk)) + 2

    try:
        for _ in range(threshold_chunks):
            buf.write(chunk)
        assert buf.using_disk is True, "should have switched to disk"
        # Total bytes match — no data lost in the switch
        assert buf.bytes_written == threshold_chunks * len(chunk)
        # File on disk has all the bytes
        assert os.path.getsize(buf.get_path()) == buf.bytes_written
    finally:
        buf.cleanup()


def test_get_data_raises_when_using_disk():
    buf = StreamingFileUpload(expected_size=MAX_MEMORY_SIZE + 1)
    try:
        with pytest.raises(ValueError):
            buf.get_data()
    finally:
        buf.cleanup()


def test_get_path_forces_switch_to_disk():
    buf = StreamingFileUpload()  # starts in memory
    buf.write(b"small data")
    try:
        assert buf.using_disk is False
        path = buf.get_path()
        assert buf.using_disk is True
        # Bytes flushed to disk
        assert os.path.getsize(path) == 10
    finally:
        buf.cleanup()


def test_cleanup_removes_tempfile():
    buf = StreamingFileUpload(expected_size=MAX_MEMORY_SIZE + 1)
    path = buf.temp_path
    assert os.path.exists(path)
    buf.cleanup()
    assert not os.path.exists(path)


def test_cleanup_idempotent_on_second_call():
    buf = StreamingFileUpload(expected_size=MAX_MEMORY_SIZE + 1)
    buf.cleanup()
    # Second cleanup must not raise even though file is gone
    buf.cleanup()


def test_write_after_close_raises():
    buf = StreamingFileUpload()
    buf.write(b"abc")
    buf.close()
    with pytest.raises(ValueError, match="closed"):
        buf.write(b"more")
    buf.cleanup()


def test_context_manager_closes_on_exit():
    with StreamingFileUpload() as buf:
        buf.write(b"in-context")
        assert buf.closed is False
    assert buf.closed is True
    buf.cleanup()


def test_data_preserved_across_memory_to_disk_promotion():
    """Bytes written before the switch must still be readable after."""
    buf = StreamingFileUpload()
    prefix = b"PREFIX-" * 100  # small, stays in memory
    buf.write(prefix)
    # Force promotion via a big single write
    big = b"X" * (MAX_MEMORY_SIZE + 1)
    buf.write(big)
    try:
        assert buf.using_disk is True
        path = buf.get_path()
        with open(path, 'rb') as f:
            data = f.read()
        assert data.startswith(prefix)
        assert len(data) == len(prefix) + len(big)
    finally:
        buf.cleanup()
