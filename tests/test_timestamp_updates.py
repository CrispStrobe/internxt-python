"""Internxt sets timestamps only when an item is created or uploaded.

drive-server-wip: PUT /folders/{uuid}/meta takes only plainName (rename),
PUT /files/{uuid}/meta only plainName/type, and replacing a file requires a
new network fileId. The client must not send a disguised rename.
"""
from unittest.mock import patch

import pytest

from services.drive import TimestampUpdateUnsupported, drive_service


@pytest.mark.parametrize('method', ['set_folder_timestamps', 'set_file_timestamps'])
@pytest.mark.parametrize('kwargs', [
    {'modification_time': '2021-06-01T08:15:30Z'},
    {'creation_time': '2021-06-01T08:15:30Z'},
    {'creation_time': '2020-01-01T00:00:00Z', 'modification_time': '2021-01-01T00:00:00Z'},
])
def test_existing_item_timestamps_are_unsupported_and_send_nothing(method, kwargs):
    with patch.object(drive_service.api, '_make_request') as request:
        with pytest.raises(TimestampUpdateUnsupported) as err:
            getattr(drive_service, method)('uuid-1', **kwargs)
    request.assert_not_called()
    assert 'only be set when it is created or uploaded' in str(err.value)


@pytest.mark.parametrize('method', ['set_folder_timestamps', 'set_file_timestamps'])
def test_missing_times_is_still_a_value_error(method):
    with pytest.raises(ValueError):
        getattr(drive_service, method)('uuid-1')


def test_unsupported_is_a_not_implemented_error():
    assert issubclass(TimestampUpdateUnsupported, NotImplementedError)
