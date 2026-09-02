"""
Tests for module-level helper functions in ing_lib_cosmos.cosmos.
"""
import json
import requests
import pytest

from ing_lib_cosmos import cosmos
from ing_lib_cosmos.cosmos import (
    CosmosRequestError, CosmosRPCError, CosmosTimeoutError,
    CosmosConnectionError, IngeniumCosmosError,
)


# ---------------------------------------------------------------------------
# _resolve_bool_env_param
# ---------------------------------------------------------------------------

class TestResolveBoolEnvParam:
    def test_explicit_bool_true(self):
        assert cosmos._resolve_bool_env_param(True, 'SOME_VAR') is True

    def test_explicit_bool_false(self):
        assert cosmos._resolve_bool_env_param(False, 'SOME_VAR') is False

    @pytest.mark.parametrize('raw', ['true', '1', 'yes', 'on', 'TRUE', 'On'])
    def test_env_true_variants(self, monkeypatch, raw):
        monkeypatch.setenv('SOME_VAR', raw)
        assert cosmos._resolve_bool_env_param(None, 'SOME_VAR') is True

    @pytest.mark.parametrize('raw', ['false', '0', 'no', 'off', 'FALSE', 'Off'])
    def test_env_false_variants(self, monkeypatch, raw):
        monkeypatch.setenv('SOME_VAR', raw)
        assert cosmos._resolve_bool_env_param(None, 'SOME_VAR') is False

    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv('SOME_VAR', raising=False)
        assert cosmos._resolve_bool_env_param(None, 'SOME_VAR', default=True) is True
        assert cosmos._resolve_bool_env_param(None, 'SOME_VAR', default=False) is False

    def test_invalid_env_string_raises(self, monkeypatch):
        monkeypatch.setenv('SOME_VAR', 'maybe')
        with pytest.raises(ValueError):
            cosmos._resolve_bool_env_param(None, 'SOME_VAR')

    def test_non_bool_param_value_normalized(self):
        # Caller passes a string instead of a bool
        assert cosmos._resolve_bool_env_param('true', 'SOME_VAR') is True
        assert cosmos._resolve_bool_env_param('false', 'SOME_VAR') is False


# ---------------------------------------------------------------------------
# _resolve_env_param
# ---------------------------------------------------------------------------

class TestResolveEnvParam:
    def test_explicit_value_used(self, monkeypatch):
        monkeypatch.setenv('SOME_VAR', 'from_env')
        assert cosmos._resolve_env_param('explicit', 'SOME_VAR') == 'explicit'

    def test_env_fallback(self, monkeypatch):
        monkeypatch.setenv('SOME_VAR', 'from_env')
        assert cosmos._resolve_env_param(None, 'SOME_VAR') == 'from_env'

    def test_missing_raises(self, monkeypatch):
        monkeypatch.delenv('SOME_VAR', raising=False)
        with pytest.raises(ValueError):
            cosmos._resolve_env_param(None, 'SOME_VAR')


# ---------------------------------------------------------------------------
# _extract_jsonrpc_error / _extract_jsonrpc_error_message
# ---------------------------------------------------------------------------

class TestExtractJsonRpcError:
    def test_no_error_key(self):
        assert cosmos._extract_jsonrpc_error({'result': 'ok'}) is None

    def test_error_key_present_dict_message(self):
        parsed = {'error': {'message': 'boom'}}
        assert cosmos._extract_jsonrpc_error(parsed) == 'RPC Error: boom'

    def test_error_with_nested_data_message(self):
        parsed = {'error': {'message': 'boom', 'data': {'message': 'inner detail'}}}
        result = cosmos._extract_jsonrpc_error(parsed)
        assert result == 'RPC Error: boom - Data Error: inner detail'

    def test_error_not_a_dict(self):
        parsed = {'error': 'just a string'}
        result = cosmos._extract_jsonrpc_error(parsed)
        assert result == 'RPC Error: just a string'

    def test_parsed_not_a_dict(self):
        assert cosmos._extract_jsonrpc_error(['not', 'a', 'dict']) is None


class TestExtractJsonRpcErrorMessage:
    def test_valid_json_with_error(self):
        text = json.dumps({'error': {'message': 'boom'}})
        assert cosmos._extract_jsonrpc_error_message(text) == 'RPC Error: boom'

    def test_valid_json_without_error(self):
        text = json.dumps({'result': 'ok'})
        assert cosmos._extract_jsonrpc_error_message(text) is None

    def test_malformed_json(self):
        assert cosmos._extract_jsonrpc_error_message('not json at all') is None


# ---------------------------------------------------------------------------
# _parse_jsonrpc
# ---------------------------------------------------------------------------

class TestParseJsonRpc:
    def test_result_present(self, response_factory):
        response = response_factory(status_code=200, json_data={'result': [1, 2, 3]})
        result = cosmos._parse_jsonrpc(response, 'fail prefix')
        assert result == [1, 2, 3]

    def test_error_present_dict_raises_rpc_error(self, response_factory):
        response = response_factory(status_code=200, json_data={'error': {'message': 'bad thing'}})
        with pytest.raises(CosmosRPCError) as excinfo:
            cosmos._parse_jsonrpc(response, 'fail prefix')
        assert 'bad thing' in str(excinfo.value)
        assert str(excinfo.value).startswith('fail prefix')
        assert excinfo.value.rpc_error == {'message': 'bad thing'}

    def test_error_present_non_dict_raises_rpc_error(self, response_factory):
        response = response_factory(status_code=200, json_data={'error': 'oops'})
        with pytest.raises(CosmosRPCError) as excinfo:
            cosmos._parse_jsonrpc(response, 'fail prefix')
        assert 'Unknown error' in str(excinfo.value)

    def test_invalid_json_body_raises_request_error(self, response_factory):
        response = response_factory(status_code=200, json_raises=True, text='<not json>')
        with pytest.raises(CosmosRequestError) as excinfo:
            cosmos._parse_jsonrpc(response, 'fail prefix')
        assert excinfo.value.args[0] == 'Failed to parse JSON response'
        assert excinfo.value.response_text == '<not json>'

    def test_unexpected_format_raises_request_error(self, response_factory):
        response = response_factory(status_code=200, json_data={'nothing': 'useful'}, text='{"nothing": "useful"}')
        with pytest.raises(CosmosRequestError) as excinfo:
            cosmos._parse_jsonrpc(response, 'fail prefix')
        assert excinfo.value.args[0] == 'Unexpected response format'


# ---------------------------------------------------------------------------
# _request
# ---------------------------------------------------------------------------

class TestRequest:
    def test_success(self, response_factory):
        response = response_factory(status_code=200, json_data={'ok': True})
        verb = lambda url, timeout, **kwargs: response
        result = cosmos._request(verb, 'http://example.com', 10)
        assert result is response

    def test_timeout_raises_cosmos_timeout_error(self):
        def verb(url, timeout, **kwargs):
            raise requests.exceptions.Timeout()
        with pytest.raises(CosmosTimeoutError) as excinfo:
            cosmos._request(verb, 'http://example.com', 10)
        assert excinfo.value.url == 'http://example.com'
        assert excinfo.value.timeout == 10

    def test_connection_error_raises_cosmos_connection_error(self):
        def verb(url, timeout, **kwargs):
            raise requests.exceptions.ConnectionError()
        with pytest.raises(CosmosConnectionError) as excinfo:
            cosmos._request(verb, 'http://example.com', 10)
        assert excinfo.value.url == 'http://example.com'

    def test_generic_exception_raises_ingenium_cosmos_error(self):
        def verb(url, timeout, **kwargs):
            raise RuntimeError('unexpected boom')
        with pytest.raises(IngeniumCosmosError) as excinfo:
            cosmos._request(verb, 'http://example.com', 10)
        assert 'unexpected boom' in str(excinfo.value)

    def test_cosmos_error_passthrough_not_wrapped(self):
        def verb(url, timeout, **kwargs):
            raise CosmosTimeoutError('already typed', url='http://example.com', timeout=10)
        with pytest.raises(CosmosTimeoutError):
            cosmos._request(verb, 'http://example.com', 10)
