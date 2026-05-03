"""Tests for config/config.py."""
from pathlib import Path

from config.config import config_service


def test_drive_api_url_is_https():
    val = config_service.get('DRIVE_NEW_API_URL')
    assert val.startswith('https://')
    assert '/drive' in val


def test_network_url_resolves_to_active_value():
    # Regression for the duplicate-key bug: the second NETWORK_URL ('api.internxt.com')
    # was the one Python actually used. We've removed the duplicate; the active value
    # must still be the api.internxt.com base used by all upload/download endpoints.
    val = config_service.get('NETWORK_URL')
    assert val == 'https://api.internxt.com'


def test_config_dict_has_no_duplicate_keys():
    """Ensures the dict-literal fix in config.py still holds."""
    src = (Path(__file__).resolve().parent.parent / 'config' / 'config.py').read_text()
    # Each key should appear at most once in the self.config dict literal.
    for key in ('DRIVE_NEW_API_URL', 'NETWORK_URL', 'APP_CRYPTO_SECRET',
                'APP_MAGIC_IV', 'APP_MAGIC_SALT'):
        # Quoted form, e.g. 'NETWORK_URL':
        count = src.count(f"'{key}':") + src.count(f"'{key}' :")
        assert count == 1, f"duplicate key in self.config: {key} (found {count})"


def test_get_unknown_key_raises():
    import pytest
    with pytest.raises(ValueError):
        config_service.get('DEFINITELY_NOT_A_KEY_xyz')
