"""
Shared pytest fixtures for the ing_lib_cosmos test suite.
"""
import json
from unittest.mock import MagicMock

import pytest

COSMOS_ENV_VARS = [
    'COSMOS_URL',
    'COSMOS_SCOPE',
    'COSMOS_USERNAME',
    'COSMOS_PASSWORD',
    'COSMOS_VERIFY_SSL',
    'COSMOS_AUTH_MODE',
]


@pytest.fixture(autouse=True)
def clean_cosmos_env(monkeypatch):
    """Ensure COSMOS_* environment variables never leak between tests."""
    for var in COSMOS_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.fixture
def full_env(monkeypatch):
    """Set a complete, valid set of COSMOS_* environment variables (enterprise mode)."""
    monkeypatch.setenv('COSMOS_URL', 'http://cosmos.example.com')
    monkeypatch.setenv('COSMOS_SCOPE', 'DEFAULT')
    monkeypatch.setenv('COSMOS_USERNAME', 'test_user')
    monkeypatch.setenv('COSMOS_PASSWORD', 'test_pass')
    monkeypatch.setenv('COSMOS_VERIFY_SSL', 'false')
    monkeypatch.setenv('COSMOS_AUTH_MODE', 'enterprise')
    return {
        'base_url': 'http://cosmos.example.com',
        'scope': 'DEFAULT',
        'username': 'test_user',
        'password': 'test_pass',
    }


def make_response(status_code=200, json_data=None, text=None, json_raises=False):
    """
    Build a MagicMock standing in for a requests.Response.

    Args:
        status_code: HTTP status code
        json_data: Value returned by .json() (ignored if json_raises)
        text: Value for .text (defaults to str(json_data) if not provided)
        json_raises: If True, .json() raises ValueError (simulating bad JSON)
    """
    response = MagicMock()
    response.status_code = status_code
    if json_raises:
        # requests raises json.JSONDecodeError (a ValueError subclass) on
        # malformed bodies; mirror that so `except json.JSONDecodeError`
        # blocks in the code under test behave as they would in production.
        response.json.side_effect = json.JSONDecodeError('Expecting value', text or '', 0)
    else:
        response.json.return_value = json_data
    response.text = text if text is not None else (str(json_data) if json_data is not None else '')
    return response


@pytest.fixture
def response_factory():
    return make_response
