"""Tests for drive_service._upload_chunk_with_progress (PUT to pre-signed URL)."""
from unittest.mock import MagicMock, patch

import pytest
import requests

from services.drive import drive_service


@pytest.fixture
def mock_session_put():
    """Patch requests.Session().put on a per-test basis."""
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.raise_for_status.return_value = None

    fake_session = MagicMock()
    fake_session.put.return_value = fake_resp

    with patch.object(requests, 'Session', return_value=fake_session):
        yield fake_session


# ---------- small chunk (direct PUT) ----------

def test_small_chunk_uploads_in_a_single_put(mock_session_put):
    drive_service._upload_chunk_with_progress("https://upload/", b"x" * 100, 60)
    mock_session_put.put.assert_called_once()
    args, kwargs = mock_session_put.put.call_args
    assert args[0] == "https://upload/"
    assert kwargs['data'] == b"x" * 100
    assert kwargs['timeout'] == 60
    assert kwargs['headers']['Content-Type'] == 'application/octet-stream'


def test_small_chunk_raises_on_non_2xx(mock_session_put):
    err_resp = MagicMock()
    err_resp.raise_for_status.side_effect = requests.exceptions.HTTPError("500")
    mock_session_put.put.return_value = err_resp
    with pytest.raises(requests.exceptions.HTTPError):
        drive_service._upload_chunk_with_progress("https://u/", b"small", 60)


# ---------- large chunk (>10MB, streamed in 1MB sub-chunks) ----------

def test_large_chunk_streams_data_via_generator(mock_session_put):
    """Files >10MB stream a generator instead of sending all bytes at once."""
    big_data = b"X" * (15 * 1024 * 1024)  # 15 MB
    drive_service._upload_chunk_with_progress("https://upload/", big_data, 300)

    args, kwargs = mock_session_put.put.call_args
    # 'data' must be an iterator/generator, not bytes
    data_arg = kwargs['data']
    assert not isinstance(data_arg, bytes), "large chunk should stream, not send all bytes at once"
    # Iterating the generator must yield exactly the original bytes
    streamed = b"".join(data_arg)
    assert streamed == big_data


def test_large_chunk_iterator_yields_1mb_pieces(mock_session_put):
    """Streaming chunk size is 1MB so progress bar updates smoothly."""
    big_data = b"Z" * (12 * 1024 * 1024)  # 12 MB

    captured_pieces = []
    def fake_put(url, data=None, **kwargs):
        # Pull the generator and record piece sizes
        for piece in data:
            captured_pieces.append(len(piece))
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status.return_value = None
        return resp
    mock_session_put.put.side_effect = fake_put

    drive_service._upload_chunk_with_progress("https://upload/", big_data, 300)

    # Most pieces should be 1MB, the last may be smaller
    assert all(p <= 1024 * 1024 for p in captured_pieces)
    assert sum(captured_pieces) == len(big_data)


def test_large_chunk_passes_correct_timeout(mock_session_put):
    big_data = b"Y" * (15 * 1024 * 1024)
    drive_service._upload_chunk_with_progress("https://upload/", big_data, 600)
    _, kwargs = mock_session_put.put.call_args
    assert kwargs['timeout'] == 600


def test_large_chunk_propagates_http_error(mock_session_put):
    """Generator path must also surface 5xx errors."""
    big_data = b"Q" * (11 * 1024 * 1024)
    err_resp = MagicMock()
    err_resp.raise_for_status.side_effect = requests.exceptions.HTTPError("503")

    def fake_put(url, data=None, **kwargs):
        # Drain the generator (mimics what the requests library does)
        for _piece in data:
            pass
        return err_resp
    mock_session_put.put.side_effect = fake_put

    with pytest.raises(requests.exceptions.HTTPError):
        drive_service._upload_chunk_with_progress("https://u/", big_data, 300)


# ---------- threshold edge cases ----------

def test_chunk_exactly_at_10mb_uses_direct_put(mock_session_put):
    """The threshold is 'len > 10MB' (strict greater-than) → exactly 10MB takes the small path."""
    data = b"a" * (10 * 1024 * 1024)
    drive_service._upload_chunk_with_progress("https://u/", data, 60)
    _, kwargs = mock_session_put.put.call_args
    # Direct path passes raw bytes
    assert kwargs['data'] == data


def test_chunk_just_over_10mb_uses_streaming(mock_session_put):
    data = b"b" * (10 * 1024 * 1024 + 1)
    drive_service._upload_chunk_with_progress("https://u/", data, 60)
    _, kwargs = mock_session_put.put.call_args
    # Streaming path passes a generator
    assert not isinstance(kwargs['data'], bytes)
