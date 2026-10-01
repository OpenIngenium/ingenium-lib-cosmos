"""
Tests for ing_lib_cosmos.cosmos.CosmosAuth
"""
import time
import pytest

from ing_lib_cosmos.cosmos import (
    CosmosAuth, AUTH_MODE_CORE, AUTH_MODE_ENTERPRISE, CosmosAuthError,
    _sanitize_response_text,
)


# ---------------------------------------------------------------------------
# Logging safety
# ---------------------------------------------------------------------------

class TestLoggingSafety:
    def test_auth_response_is_redacted(self):
        body = '{"access_token":"access","refresh_token":"refresh"}'

        assert _sanitize_response_text(body, 'http://cosmos/auth/token') == '<redacted>'

    def test_sensitive_json_fields_are_redacted(self):
        body = '{"result":{"token":"secret","value":1}}'

        sanitized = _sanitize_response_text(body, 'http://cosmos/api')

        assert 'secret' not in sanitized
        assert '"value": 1' in sanitized


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------

class TestConstructor:
    def test_env_fallbacks(self, full_env):
        auth = CosmosAuth()
        assert auth.base_url == full_env['base_url']
        assert auth.username == full_env['username']
        assert auth.password == full_env['password']
        assert auth.verify_ssl is False
        assert auth.auth_mode == AUTH_MODE_ENTERPRISE

    def test_explicit_args_override_env(self, full_env):
        auth = CosmosAuth(base_url='http://other.example.com', username='u2', password='p2', verify_ssl=True)
        assert auth.base_url == 'http://other.example.com'
        assert auth.username == 'u2'
        assert auth.password == 'p2'
        assert auth.verify_ssl is True

    def test_missing_base_url_raises(self, monkeypatch):
        monkeypatch.setenv('COSMOS_PASSWORD', 'p')
        with pytest.raises(ValueError):
            CosmosAuth()

    def test_missing_password_raises(self, monkeypatch):
        monkeypatch.setenv('COSMOS_URL', 'http://x')
        with pytest.raises(ValueError):
            CosmosAuth()

    def test_invalid_auth_mode_raises(self, monkeypatch):
        monkeypatch.setenv('COSMOS_URL', 'http://x')
        monkeypatch.setenv('COSMOS_PASSWORD', 'p')
        with pytest.raises(ValueError):
            CosmosAuth(auth_mode='bogus')

    def test_enterprise_requires_username(self, monkeypatch):
        monkeypatch.setenv('COSMOS_URL', 'http://x')
        monkeypatch.setenv('COSMOS_PASSWORD', 'p')
        with pytest.raises(ValueError):
            CosmosAuth(auth_mode=AUTH_MODE_ENTERPRISE)

    def test_core_mode_does_not_require_username(self, monkeypatch):
        monkeypatch.setenv('COSMOS_URL', 'http://x')
        monkeypatch.setenv('COSMOS_PASSWORD', 'p')
        auth = CosmosAuth(auth_mode=AUTH_MODE_CORE)
        assert auth.username is None
        assert auth.auth_mode == AUTH_MODE_CORE

    def test_base_url_trailing_slash_stripped(self, monkeypatch):
        monkeypatch.setenv('COSMOS_PASSWORD', 'p')
        auth = CosmosAuth(base_url='http://x/', username='u', auth_mode=AUTH_MODE_ENTERPRISE)
        assert auth.base_url == 'http://x'


# ---------------------------------------------------------------------------
# get_token_enterprise
# ---------------------------------------------------------------------------

class TestGetTokenEnterprise:
    def test_success(self, full_env, mocker, response_factory):
        response = response_factory(status_code=200, json_data={
            'access_token': 'tok123', 'refresh_token': 'ref123', 'expires_in': 300
        })
        mocker.patch('ing_lib_cosmos.cosmos.requests.post', return_value=response)

        auth = CosmosAuth()
        result = auth.get_token_enterprise()

        assert result == 'tok123'
        assert auth.access_token == 'tok123'
        assert auth.refresh_token == 'ref123'
        assert auth.token_expiry > time.time()

    def test_failure_without_rpc_message(self, full_env, mocker, response_factory):
        response = response_factory(status_code=401, text='plain text error', json_raises=True)
        mocker.patch('ing_lib_cosmos.cosmos.requests.post', return_value=response)

        auth = CosmosAuth()
        with pytest.raises(CosmosAuthError) as excinfo:
            auth.get_token_enterprise()
        assert excinfo.value.status_code == 401

    def test_failure_with_rpc_message(self, full_env, mocker, response_factory):
        import json
        body = json.dumps({'error': {'message': 'invalid credentials'}})
        response = response_factory(status_code=403, json_data={'error': {'message': 'invalid credentials'}}, text=body)
        mocker.patch('ing_lib_cosmos.cosmos.requests.post', return_value=response)

        auth = CosmosAuth()
        with pytest.raises(CosmosAuthError) as excinfo:
            auth.get_token_enterprise()
        assert 'invalid credentials' in str(excinfo.value)

    def test_transport_error_propagates(self, full_env, mocker):
        import requests as real_requests
        from ing_lib_cosmos.cosmos import CosmosTimeoutError
        mocker.patch('ing_lib_cosmos.cosmos.requests.post', side_effect=real_requests.exceptions.Timeout())

        auth = CosmosAuth()
        with pytest.raises(CosmosTimeoutError):
            auth.get_token_enterprise()


# ---------------------------------------------------------------------------
# refresh_access_token
# ---------------------------------------------------------------------------

class TestRefreshAccessToken:
    def test_no_refresh_token(self, full_env):
        auth = CosmosAuth()
        with pytest.raises(CosmosAuthError) as excinfo:
            auth.refresh_access_token()
        assert excinfo.value.status_code == 400

    def test_success(self, full_env, mocker, response_factory):
        auth = CosmosAuth()
        auth.refresh_token = 'old_refresh'

        response = response_factory(status_code=200, json_data={
            'access_token': 'new_tok', 'refresh_token': 'new_ref', 'expires_in': 300
        })
        mocker.patch('ing_lib_cosmos.cosmos.requests.post', return_value=response)

        result = auth.refresh_access_token()

        assert result == 'new_tok'
        assert auth.access_token == 'new_tok'
        assert auth.refresh_token == 'new_ref'

    def test_failure(self, full_env, mocker, response_factory):
        auth = CosmosAuth()
        auth.refresh_token = 'old_refresh'

        response = response_factory(status_code=401, text='invalid_grant', json_raises=True)
        mocker.patch('ing_lib_cosmos.cosmos.requests.post', return_value=response)

        with pytest.raises(CosmosAuthError) as excinfo:
            auth.refresh_access_token()
        assert excinfo.value.status_code == 401


# ---------------------------------------------------------------------------
# get_token_core
# ---------------------------------------------------------------------------

class TestGetTokenCore:
    def test_success(self, monkeypatch, mocker, response_factory):
        monkeypatch.setenv('COSMOS_URL', 'http://x')
        monkeypatch.setenv('COSMOS_PASSWORD', 'corepass')
        auth = CosmosAuth(auth_mode=AUTH_MODE_CORE)

        response = response_factory(status_code=200, text='"sometoken"')
        mocker.patch('ing_lib_cosmos.cosmos.requests.post', return_value=response)

        result = auth.get_token_core()

        assert result == 'sometoken'
        assert auth.access_token == 'sometoken'
        assert auth.token_expiry == float('inf')

    def test_failure(self, monkeypatch, mocker, response_factory):
        monkeypatch.setenv('COSMOS_URL', 'http://x')
        monkeypatch.setenv('COSMOS_PASSWORD', 'corepass')
        auth = CosmosAuth(auth_mode=AUTH_MODE_CORE)

        response = response_factory(status_code=401, text='unauthorized', json_raises=True)
        mocker.patch('ing_lib_cosmos.cosmos.requests.post', return_value=response)

        with pytest.raises(CosmosAuthError) as excinfo:
            auth.get_token_core()
        assert excinfo.value.status_code == 401


# ---------------------------------------------------------------------------
# get_valid_token
# ---------------------------------------------------------------------------

class TestGetValidToken:
    def test_cached_token_short_circuits(self, full_env, mocker):
        auth = CosmosAuth()
        auth.access_token = 'cached'
        auth.token_expiry = time.time() + 100
        post_mock = mocker.patch('ing_lib_cosmos.cosmos.requests.post')

        result = auth.get_valid_token()

        assert result == 'cached'
        post_mock.assert_not_called()

    def test_core_mode_dispatch(self, monkeypatch, mocker, response_factory):
        monkeypatch.setenv('COSMOS_URL', 'http://x')
        monkeypatch.setenv('COSMOS_PASSWORD', 'corepass')
        auth = CosmosAuth(auth_mode=AUTH_MODE_CORE)

        response = response_factory(status_code=200, text='"coretoken"')
        mocker.patch('ing_lib_cosmos.cosmos.requests.post', return_value=response)

        result = auth.get_valid_token()

        assert result == 'coretoken'

    def test_core_mode_no_password_errors(self, monkeypatch):
        monkeypatch.setenv('COSMOS_URL', 'http://x')
        monkeypatch.setenv('COSMOS_PASSWORD', 'corepass')
        auth = CosmosAuth(auth_mode=AUTH_MODE_CORE)
        auth.password = None

        with pytest.raises(CosmosAuthError):
            auth.get_valid_token()

    def test_enterprise_refresh_success(self, full_env, mocker, response_factory):
        auth = CosmosAuth()
        auth.refresh_token = 'old_ref'

        response = response_factory(status_code=200, json_data={
            'access_token': 'refreshed_tok', 'refresh_token': 'new_ref', 'expires_in': 300
        })
        mocker.patch('ing_lib_cosmos.cosmos.requests.post', return_value=response)

        result = auth.get_valid_token()

        assert result == 'refreshed_tok'

    def test_enterprise_refresh_fails_then_gets_new_token(self, full_env, mocker, response_factory):
        auth = CosmosAuth()
        auth.refresh_token = 'bad_ref'

        fail_response = response_factory(status_code=401, text='invalid_grant', json_raises=True)
        success_response = response_factory(status_code=200, json_data={
            'access_token': 'brand_new_tok', 'refresh_token': 'brand_new_ref', 'expires_in': 300
        })
        mocker.patch('ing_lib_cosmos.cosmos.requests.post', side_effect=[fail_response, success_response])

        result = auth.get_valid_token()

        assert result == 'brand_new_tok'

    def test_missing_credentials_errors(self, monkeypatch):
        monkeypatch.setenv('COSMOS_URL', 'http://x')
        monkeypatch.setenv('COSMOS_PASSWORD', 'p')
        monkeypatch.setenv('COSMOS_USERNAME', 'u')
        auth = CosmosAuth()
        auth.username = None
        auth.password = None

        with pytest.raises(CosmosAuthError) as excinfo:
            auth.get_valid_token()
        assert excinfo.value.status_code == 400


# ---------------------------------------------------------------------------
# invalidate
# ---------------------------------------------------------------------------

class TestInvalidate:
    def test_clears_state(self, full_env):
        auth = CosmosAuth()
        auth.access_token = 'tok'
        auth.refresh_token = 'ref'
        auth.token_expiry = time.time() + 100

        auth.invalidate()

        assert auth.access_token is None
        assert auth.refresh_token is None
        assert auth.token_expiry == 0
