"""
Tests for ing_lib_cosmos.cosmos.CosmosAPIClient
"""
import time
import json as json_module

import pytest

from ing_lib_cosmos.cosmos import (
    CosmosAPIClient, CosmosAuth, IngeniumCosmosError, CosmosAuthError,
    CosmosRequestError, CosmosRPCError, CosmosScriptError,
    SCRIPT_FILTER_RUNNING, SCRIPT_FILTER_COMPLETED, SCRIPT_FILTER_BOTH,
)


def make_client(mocker, full_env, access_token='initial_tok'):
    """
    Build a CosmosAPIClient with a mocked, already-authenticated CosmosAuth
    so tests can focus on the client behavior without re-testing auth flow.
    """
    client = CosmosAPIClient()
    mocker.patch.object(client.auth, 'get_valid_token', return_value=access_token)
    return client


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------

class TestConstructor:
    def test_env_fallbacks(self, full_env):
        client = CosmosAPIClient()
        assert client.base_url == full_env['base_url']
        assert client.scope == full_env['scope']
        assert client.verify_ssl is False
        assert client.token is None
        assert 'Content-Type' in client.headers

    def test_missing_scope_raises(self, monkeypatch):
        monkeypatch.setenv('COSMOS_URL', 'http://x')
        monkeypatch.setenv('COSMOS_PASSWORD', 'p')
        monkeypatch.setenv('COSMOS_USERNAME', 'u')
        with pytest.raises(ValueError):
            CosmosAPIClient()

    def test_shared_auth_reused(self, full_env):
        shared_auth = CosmosAuth()
        client = CosmosAPIClient(auth=shared_auth)
        assert client.auth is shared_auth

    def test_no_network_io_on_init(self, full_env, mocker):
        post_mock = mocker.patch('ing_lib_cosmos.cosmos.requests.post')
        CosmosAPIClient()
        post_mock.assert_not_called()


# ---------------------------------------------------------------------------
# login / _ensure_token
# ---------------------------------------------------------------------------

class TestEnsureToken:
    def test_login_propagates_auth_error(self, full_env, mocker):
        client = CosmosAPIClient()
        mocker.patch.object(client.auth, 'get_valid_token', side_effect=CosmosAuthError('no creds', status_code=400))
        with pytest.raises(CosmosAuthError):
            client.login()

    def test_ensure_token_updates_headers_on_change(self, full_env, mocker):
        client = make_client(mocker, full_env, access_token='new_token')
        token = client._ensure_token()
        assert token == 'new_token'
        assert client.token == 'new_token'
        assert client.headers['Authorization'] == 'Bearer new_token'
        assert client.session.headers['Authorization'] == 'Bearer new_token'

    def test_ensure_token_noop_when_unchanged(self, full_env, mocker):
        client = make_client(mocker, full_env, access_token='same_token')
        client._ensure_token()
        headers_before = dict(client.headers)
        client._ensure_token()
        assert client.headers == headers_before


# ---------------------------------------------------------------------------
# _authed_request
# ---------------------------------------------------------------------------

class TestAuthedRequest:
    def test_success(self, full_env, mocker, response_factory):
        client = make_client(mocker, full_env)
        response = response_factory(status_code=200, json_data={'result': 'ok'})
        verb = mocker.Mock(return_value=response)

        result = client._authed_request(verb, 'http://x/api', 10)

        assert result is response
        verb.assert_called_once()

    def test_401_retries_once_and_succeeds(self, full_env, mocker, response_factory):
        client = make_client(mocker, full_env)
        invalidate_mock = mocker.patch.object(client.auth, 'invalidate')

        unauthorized = response_factory(status_code=401, text='unauthorized')
        ok = response_factory(status_code=200, json_data={'result': 'ok'})
        verb = mocker.Mock(side_effect=[unauthorized, ok])

        result = client._authed_request(verb, 'http://x/api', 10)

        assert result is ok
        assert verb.call_count == 2
        invalidate_mock.assert_called_once()

    def test_401_retry_still_fails(self, full_env, mocker, response_factory):
        client = make_client(mocker, full_env)
        mocker.patch.object(client.auth, 'invalidate')

        unauthorized1 = response_factory(status_code=401, text='unauthorized')
        unauthorized2 = response_factory(status_code=403, text='still unauthorized')
        verb = mocker.Mock(side_effect=[unauthorized1, unauthorized2])

        result = client._authed_request(verb, 'http://x/api', 10)

        assert result is unauthorized2
        assert verb.call_count == 2

    def test_transport_error_short_circuits(self, full_env, mocker):
        client = make_client(mocker, full_env)
        import requests as real_requests
        from ing_lib_cosmos.cosmos import CosmosConnectionError
        verb = mocker.Mock(side_effect=real_requests.exceptions.ConnectionError())

        with pytest.raises(CosmosConnectionError):
            client._authed_request(verb, 'http://x/api', 10)

    def test_auth_failure_short_circuits(self, full_env, mocker):
        client = CosmosAPIClient()
        mocker.patch.object(client.auth, 'get_valid_token', side_effect=CosmosAuthError('no creds', status_code=400))
        verb = mocker.Mock()

        with pytest.raises(CosmosAuthError):
            client._authed_request(verb, 'http://x/api', 10)
        verb.assert_not_called()


# ---------------------------------------------------------------------------
# send_command
# ---------------------------------------------------------------------------

class TestSendCommand:
    @pytest.mark.parametrize('check,expected_method', [
        ('NO_CHECK', 'cmd_no_checks'),
        ('ENABLED', 'cmd'),
    ])
    def test_builds_correct_method(self, full_env, mocker, response_factory, check, expected_method):
        client = make_client(mocker, full_env)
        response = response_factory(status_code=200, json_data={'result': 'ok'})
        post_mock = mocker.patch('ing_lib_cosmos.cosmos.requests.post', return_value=response)

        result = client.send_command('TGT CMD', check)

        assert result == 'ok'
        payload = post_mock.call_args.kwargs['json']
        assert payload['method'] == expected_method
        assert payload['params'] == ['TGT CMD']
        assert payload['keyword_params']['scope'] == full_env['scope']

    def test_invalid_check_raises(self, full_env, mocker):
        client = make_client(mocker, full_env)
        with pytest.raises(IngeniumCosmosError):
            client.send_command('TGT CMD', 'BOGUS')

    def test_non_200_response(self, full_env, mocker, response_factory):
        client = make_client(mocker, full_env)
        response = response_factory(status_code=500, text='server error', json_raises=True)
        mocker.patch('ing_lib_cosmos.cosmos.requests.post', return_value=response)

        with pytest.raises(CosmosRequestError) as excinfo:
            client.send_command('TGT CMD', 'ENABLED')
        assert excinfo.value.status_code == 500

    def test_success_parses_jsonrpc(self, full_env, mocker, response_factory):
        client = make_client(mocker, full_env)
        response = response_factory(status_code=200, json_data={'result': 'command sent'})
        mocker.patch('ing_lib_cosmos.cosmos.requests.post', return_value=response)

        result = client.send_command('TGT CMD', 'ENABLED')

        assert result == 'command sent'

    def test_transport_error_propagates(self, full_env, mocker):
        client = make_client(mocker, full_env)
        import requests as real_requests
        from ing_lib_cosmos.cosmos import CosmosTimeoutError
        mocker.patch('ing_lib_cosmos.cosmos.requests.post', side_effect=real_requests.exceptions.Timeout())

        with pytest.raises(CosmosTimeoutError):
            client.send_command('TGT CMD', 'ENABLED')

    def test_connection_error_propagates(self, full_env, mocker):
        client = make_client(mocker, full_env)
        import requests as real_requests
        from ing_lib_cosmos.cosmos import CosmosConnectionError
        mocker.patch('ing_lib_cosmos.cosmos.requests.post', side_effect=real_requests.exceptions.ConnectionError())

        with pytest.raises(CosmosConnectionError):
            client.send_command('TGT CMD', 'ENABLED')

    def test_rpc_error_raises_cosmos_rpc_error(self, full_env, mocker, response_factory):
        """A 200 response whose JSON-RPC body contains an 'error' field
        should raise CosmosRPCError, not be treated as success."""
        client = make_client(mocker, full_env)
        response = response_factory(
            status_code=200,
            json_data={'jsonrpc': '2.0', 'id': 1, 'error': {'message': 'bad command'}},
        )
        mocker.patch('ing_lib_cosmos.cosmos.requests.post', return_value=response)

        with pytest.raises(CosmosRPCError) as excinfo:
            client.send_command('TGT CMD', 'ENABLED')
        assert 'bad command' in str(excinfo.value)

    def test_non_200_response_includes_rpc_message(self, full_env, mocker, response_factory):
        """When the non-200 body IS parseable JSON-RPC with an error, its
        message should be appended to the raised CosmosRequestError."""
        client = make_client(mocker, full_env)
        body = {'jsonrpc': '2.0', 'id': 1, 'error': {'message': 'forbidden target'}}
        response = response_factory(status_code=403, text=json_module.dumps(body))
        mocker.patch('ing_lib_cosmos.cosmos.requests.post', return_value=response)

        with pytest.raises(CosmosRequestError) as excinfo:
            client.send_command('TGT CMD', 'ENABLED')
        assert excinfo.value.status_code == 403
        assert 'forbidden target' in str(excinfo.value)

    def test_retries_once_on_401_then_succeeds(self, full_env, mocker, response_factory):
        """A 401 on the first attempt should invalidate the token and retry
        once, succeeding on the second attempt."""
        client = make_client(mocker, full_env)
        unauthorized = response_factory(status_code=401, json_data={'error': 'unauthorized'})
        success = response_factory(status_code=200, json_data={'result': 'ok'})
        post_mock = mocker.patch('ing_lib_cosmos.cosmos.requests.post',
                                  side_effect=[unauthorized, success])
        invalidate_spy = mocker.spy(client.auth, 'invalidate')

        result = client.send_command('TGT CMD', 'ENABLED')

        assert result == 'ok'
        assert post_mock.call_count == 2
        invalidate_spy.assert_called_once()


# ---------------------------------------------------------------------------
# get_telemetry
# ---------------------------------------------------------------------------

class TestGetTelemetry:
    def test_payload_and_success_zip(self, full_env, mocker, response_factory):
        client = make_client(mocker, full_env)
        tlm_points = ['INST SAMPLE TEMP1', 'INST SAMPLE TEMP2']
        response = response_factory(status_code=200, json_data={'result': [1.0, 2.0]})
        post_mock = mocker.patch('ing_lib_cosmos.cosmos.requests.post', return_value=response)

        result = client.get_telemetry(tlm_points)

        payload = post_mock.call_args.kwargs['json']
        assert payload['method'] == 'tlm'
        assert payload['params'] == [tlm_points]
        assert result == {'INST SAMPLE TEMP1': 1.0, 'INST SAMPLE TEMP2': 2.0}

    def test_non_200(self, full_env, mocker, response_factory):
        client = make_client(mocker, full_env)
        response = response_factory(status_code=502, text='bad gateway', json_raises=True)
        mocker.patch('ing_lib_cosmos.cosmos.requests.post', return_value=response)

        with pytest.raises(CosmosRequestError) as excinfo:
            client.get_telemetry(['INST SAMPLE TEMP1'])
        assert excinfo.value.status_code == 502


# ---------------------------------------------------------------------------
# query_telemetry
# ---------------------------------------------------------------------------

class TestQueryTelemetry:
    def test_start_end_time_included(self, full_env, mocker, response_factory):
        client = make_client(mocker, full_env)
        response = response_factory(status_code=200, json_data={'result': []})
        post_mock = mocker.patch('ing_lib_cosmos.cosmos.requests.post', return_value=response)

        client.query_telemetry([['INST', 'SAMPLE', 'TEMP1']], start_time='2026-01-01T00:00:00Z',
                                end_time='2026-01-01T01:00:00Z')

        payload = post_mock.call_args.kwargs['json']
        assert payload['keyword_params']['start_time'] == '2026-01-01T00:00:00Z'
        assert payload['keyword_params']['end_time'] == '2026-01-01T01:00:00Z'

    def test_start_end_time_omitted_when_not_provided(self, full_env, mocker, response_factory):
        client = make_client(mocker, full_env)
        response = response_factory(status_code=200, json_data={'result': []})
        post_mock = mocker.patch('ing_lib_cosmos.cosmos.requests.post', return_value=response)

        client.query_telemetry([['INST', 'SAMPLE', 'TEMP1']])

        payload = post_mock.call_args.kwargs['json']
        assert 'start_time' not in payload['keyword_params']
        assert 'end_time' not in payload['keyword_params']

    def test_success(self, full_env, mocker, response_factory):
        client = make_client(mocker, full_env)
        response = response_factory(status_code=200, json_data={'result': [[1, 2, 3]]})
        mocker.patch('ing_lib_cosmos.cosmos.requests.post', return_value=response)

        result = client.query_telemetry([['INST', 'SAMPLE', 'TEMP1']])
        assert result == [[1, 2, 3]]

    def test_non_200(self, full_env, mocker, response_factory):
        client = make_client(mocker, full_env)
        response = response_factory(status_code=500, text='error', json_raises=True)
        mocker.patch('ing_lib_cosmos.cosmos.requests.post', return_value=response)

        with pytest.raises(CosmosRequestError):
            client.query_telemetry([['INST', 'SAMPLE', 'TEMP1']])


# ---------------------------------------------------------------------------
# get_cmd_time
# ---------------------------------------------------------------------------

class TestGetCmdTime:
    def test_with_command(self, full_env, mocker, response_factory):
        client = make_client(mocker, full_env)
        response = response_factory(status_code=200, json_data={'result': ['SAMPLE', 'ECHO', 1000, 500000]})
        post_mock = mocker.patch('ing_lib_cosmos.cosmos.requests.post', return_value=response)

        result = client.get_cmd_time('SAMPLE', 'ECHO')

        payload = post_mock.call_args.kwargs['json']
        assert payload['params'] == ['SAMPLE', 'ECHO']
        assert result['target_name'] == 'SAMPLE'
        assert result['command_name'] == 'ECHO'
        assert result['time'] == 1000.5

    def test_without_command(self, full_env, mocker, response_factory):
        client = make_client(mocker, full_env)
        response = response_factory(status_code=200, json_data={'result': ['SAMPLE', None, 1000, 0]})
        post_mock = mocker.patch('ing_lib_cosmos.cosmos.requests.post', return_value=response)

        client.get_cmd_time('SAMPLE')

        payload = post_mock.call_args.kwargs['json']
        assert payload['params'] == ['SAMPLE']

    def test_none_time_components(self, full_env, mocker, response_factory):
        client = make_client(mocker, full_env)
        response = response_factory(status_code=200, json_data={'result': ['SAMPLE', None, None, None]})
        mocker.patch('ing_lib_cosmos.cosmos.requests.post', return_value=response)

        result = client.get_cmd_time('SAMPLE')

        assert result['time'] is None

    def test_unexpected_format_leaves_raw(self, full_env, mocker, response_factory):
        client = make_client(mocker, full_env)
        response = response_factory(status_code=200, json_data={'result': 'not-a-list'})
        mocker.patch('ing_lib_cosmos.cosmos.requests.post', return_value=response)

        result = client.get_cmd_time('SAMPLE')

        assert result == 'not-a-list'

    def test_non_200(self, full_env, mocker, response_factory):
        client = make_client(mocker, full_env)
        response = response_factory(status_code=500, text='err', json_raises=True)
        mocker.patch('ing_lib_cosmos.cosmos.requests.post', return_value=response)

        with pytest.raises(CosmosRequestError):
            client.get_cmd_time('SAMPLE')


# ---------------------------------------------------------------------------
# get_cmd_cnt
# ---------------------------------------------------------------------------

class TestGetCmdCnt:
    def test_with_command(self, full_env, mocker, response_factory):
        client = make_client(mocker, full_env)
        response = response_factory(status_code=200, json_data={'result': 5})
        post_mock = mocker.patch('ing_lib_cosmos.cosmos.requests.post', return_value=response)

        result = client.get_cmd_cnt('SAMPLE', 'ECHO')

        payload = post_mock.call_args.kwargs['json']
        assert payload['params'] == ['SAMPLE', 'ECHO']
        assert result == {'target_name': 'SAMPLE', 'command_name': 'ECHO', 'count': 5}

    def test_without_command(self, full_env, mocker, response_factory):
        client = make_client(mocker, full_env)
        response = response_factory(status_code=200, json_data={'result': 5})
        post_mock = mocker.patch('ing_lib_cosmos.cosmos.requests.post', return_value=response)

        client.get_cmd_cnt('SAMPLE')

        payload = post_mock.call_args.kwargs['json']
        assert payload['params'] == ['SAMPLE']

    def test_non_200(self, full_env, mocker, response_factory):
        client = make_client(mocker, full_env)
        response = response_factory(status_code=500, text='err', json_raises=True)
        mocker.patch('ing_lib_cosmos.cosmos.requests.post', return_value=response)

        with pytest.raises(CosmosRequestError):
            client.get_cmd_cnt('SAMPLE')


# ---------------------------------------------------------------------------
# start_script
# ---------------------------------------------------------------------------

class TestStartScript:
    def test_path_traversal_rejected(self, full_env, mocker):
        client = make_client(mocker, full_env)
        with pytest.raises(IngeniumCosmosError):
            client.start_script('../etc/passwd')

    def test_leading_slash_rejected(self, full_env, mocker):
        client = make_client(mocker, full_env)
        with pytest.raises(IngeniumCosmosError):
            client.start_script('/etc/passwd')

    def test_lock_failure_just_warns(self, full_env, mocker, response_factory):
        client = make_client(mocker, full_env)
        lock_response = response_factory(status_code=500, text='lock failed')
        run_response = response_factory(status_code=200, text='42')
        session_post = mocker.patch.object(client.session, 'post', side_effect=[lock_response, run_response])

        result = client.start_script('TARGET/procedures/script.py')

        assert result['script_id'] == 42
        assert result['running'] is True
        assert session_post.call_count == 2

    def test_run_success_parses_script_id(self, full_env, mocker, response_factory):
        client = make_client(mocker, full_env)
        lock_response = response_factory(status_code=200, text='')
        run_response = response_factory(status_code=200, text='123')
        mocker.patch.object(client.session, 'post', side_effect=[lock_response, run_response])

        result = client.start_script('TARGET/procedures/script.py')

        assert result['script_id'] == 123
        assert result['running'] is True

    def test_run_non_numeric_falls_back_to_json_error(self, full_env, mocker, response_factory):
        client = make_client(mocker, full_env)
        lock_response = response_factory(status_code=200, text='')
        run_response = response_factory(status_code=200, text='not-a-number',
                                         json_data={'error': {'message': 'script not found'}})
        mocker.patch.object(client.session, 'post', side_effect=[lock_response, run_response])

        with pytest.raises(CosmosRPCError) as excinfo:
            client.start_script('TARGET/procedures/script.py')
        assert 'script not found' in str(excinfo.value)

    def test_run_unparseable_response(self, full_env, mocker, response_factory):
        client = make_client(mocker, full_env)
        lock_response = response_factory(status_code=200, text='')
        run_response = response_factory(status_code=200, text='garbage', json_raises=True)
        mocker.patch.object(client.session, 'post', side_effect=[lock_response, run_response])

        with pytest.raises(CosmosRequestError) as excinfo:
            client.start_script('TARGET/procedures/script.py')
        assert excinfo.value.args[0] == 'Failed to parse script ID from response'

    def test_run_non_success_status(self, full_env, mocker, response_factory):
        client = make_client(mocker, full_env)
        lock_response = response_factory(status_code=200, text='')
        run_response = response_factory(status_code=500, text='server error', json_raises=True)
        mocker.patch.object(client.session, 'post', side_effect=[lock_response, run_response])

        with pytest.raises(CosmosRequestError) as excinfo:
            client.start_script('TARGET/procedures/script.py')
        assert excinfo.value.status_code == 500

    def test_lock_transport_error_warns_and_continues(self, full_env, mocker, response_factory):
        client = make_client(mocker, full_env)
        import requests as real_requests
        run_response = response_factory(status_code=200, text='55')
        session_post = mocker.patch.object(
            client.session, 'post', side_effect=[real_requests.exceptions.ConnectionError(), run_response]
        )

        result = client.start_script('TARGET/procedures/script.py')

        assert result['script_id'] == 55
        assert session_post.call_count == 2

    def test_run_transport_error_propagates(self, full_env, mocker, response_factory):
        client = make_client(mocker, full_env)
        import requests as real_requests
        from ing_lib_cosmos.cosmos import CosmosTimeoutError
        lock_response = response_factory(status_code=200, text='')
        session_post = mocker.patch.object(
            client.session, 'post', side_effect=[lock_response, real_requests.exceptions.Timeout()]
        )

        with pytest.raises(CosmosTimeoutError):
            client.start_script('TARGET/procedures/script.py')

    def test_lock_false_skips_lock_call(self, full_env, mocker, response_factory):
        client = make_client(mocker, full_env)
        run_response = response_factory(status_code=200, text='7')
        session_post = mocker.patch.object(client.session, 'post', return_value=run_response)

        result = client.start_script('TARGET/procedures/script.py', lock=False)

        assert result['script_id'] == 7
        session_post.assert_called_once()


# ---------------------------------------------------------------------------
# get_script
# ---------------------------------------------------------------------------

class TestGetScript:
    def test_running_found(self, full_env, mocker, response_factory):
        client = make_client(mocker, full_env)
        running_response = response_factory(status_code=200, json_data={'state': 'running', 'line_no': 5})
        mocker.patch.object(client.session, 'get', return_value=running_response)

        result = client.get_script(1)

        assert result['found'] is True
        assert result['running'] is True
        assert result['state'] == 'running'
        assert result['line_no'] == 5

    def test_completed_found_after_running_404(self, full_env, mocker, response_factory):
        client = make_client(mocker, full_env)
        running_response = response_factory(status_code=404, text='not found')
        completed_response = response_factory(status_code=200, json_data={'state': 'completed', 'line_no': 10})
        mocker.patch.object(client.session, 'get', side_effect=[running_response, completed_response])

        result = client.get_script(1)

        assert result['found'] is True
        assert result['running'] is False
        assert result['state'] == 'completed'

    def test_running_json_decode_error_falls_through_to_completed(self, full_env, mocker, response_factory):
        client = make_client(mocker, full_env)
        running_response = response_factory(status_code=200, text='not-json', json_raises=True)
        completed_response = response_factory(status_code=200, json_data={'state': 'completed', 'line_no': 4})
        mocker.patch.object(client.session, 'get', side_effect=[running_response, completed_response])

        result = client.get_script(1)

        assert result['found'] is True
        assert result['running'] is False
        assert result['state'] == 'completed'

    def test_not_found_in_either(self, full_env, mocker, response_factory):
        client = make_client(mocker, full_env)
        not_found = response_factory(status_code=404, text='not found')
        mocker.patch.object(client.session, 'get', side_effect=[not_found, not_found])

        result = client.get_script(1)

        assert result['found'] is False
        assert result['running'] is False


# ---------------------------------------------------------------------------
# get_all_scripts
# ---------------------------------------------------------------------------

class TestGetAllScripts:
    def test_both_default(self, full_env, mocker, response_factory):
        client = make_client(mocker, full_env)
        running_response = response_factory(status_code=200, json_data={'items': [{'id': 1}], 'total': 1})
        completed_response = response_factory(status_code=200, json_data={'items': [{'id': 2}], 'total': 1})
        mocker.patch.object(client.session, 'get', side_effect=[running_response, completed_response])

        result = client.get_all_scripts()

        assert result['running_scripts'] == [{'id': 1}]
        assert result['completed_scripts'] == [{'id': 2}]

    def test_running_only(self, full_env, mocker, response_factory):
        client = make_client(mocker, full_env)
        running_response = response_factory(status_code=200, json_data={'items': [{'id': 1}], 'total': 1})
        get_mock = mocker.patch.object(client.session, 'get', return_value=running_response)

        result = client.get_all_scripts(SCRIPT_FILTER_RUNNING)

        assert result == {'running_scripts': [{'id': 1}]}
        get_mock.assert_called_once()

    def test_completed_only(self, full_env, mocker, response_factory):
        client = make_client(mocker, full_env)
        completed_response = response_factory(status_code=200, json_data={'items': [{'id': 2}], 'total': 1})
        get_mock = mocker.patch.object(client.session, 'get', return_value=completed_response)

        result = client.get_all_scripts(SCRIPT_FILTER_COMPLETED)

        assert result == {'completed_scripts': [{'id': 2}]}
        get_mock.assert_called_once()

    def test_invalid_filter_raises(self, full_env, mocker):
        client = make_client(mocker, full_env)
        with pytest.raises(ValueError):
            client.get_all_scripts('bogus')

    def test_running_failure_raises(self, full_env, mocker, response_factory):
        client = make_client(mocker, full_env)
        error_response = response_factory(status_code=500, text='server error', json_raises=True)
        mocker.patch.object(client.session, 'get', return_value=error_response)

        with pytest.raises(IngeniumCosmosError):
            client.get_all_scripts(SCRIPT_FILTER_RUNNING)

    def test_completed_failure_raises(self, full_env, mocker, response_factory):
        client = make_client(mocker, full_env)
        error_response = response_factory(status_code=500, text='server error', json_raises=True)
        mocker.patch.object(client.session, 'get', return_value=error_response)

        with pytest.raises(IngeniumCosmosError):
            client.get_all_scripts(SCRIPT_FILTER_COMPLETED)


# ---------------------------------------------------------------------------
# halt_script
# ---------------------------------------------------------------------------

class TestHaltScript:
    @pytest.mark.parametrize('status_code', [200, 201, 204])
    def test_success_statuses(self, full_env, mocker, response_factory, status_code):
        client = make_client(mocker, full_env)
        response = response_factory(status_code=status_code, text='')
        mocker.patch.object(client.session, 'post', return_value=response)

        result = client.halt_script(1)

        assert result['running'] is False
        assert result['already_stopped'] is False

    def test_404_treated_as_already_stopped(self, full_env, mocker, response_factory):
        client = make_client(mocker, full_env)
        response = response_factory(status_code=404, text='not running')
        mocker.patch.object(client.session, 'post', return_value=response)

        result = client.halt_script(1)

        assert result['running'] is False
        assert result['already_stopped'] is True

    def test_other_status_errors(self, full_env, mocker, response_factory):
        client = make_client(mocker, full_env)
        response = response_factory(status_code=500, text='server error', json_raises=True)
        mocker.patch.object(client.session, 'post', return_value=response)

        with pytest.raises(CosmosScriptError) as excinfo:
            client.halt_script(1)
        assert excinfo.value.script_id == 1

    def test_transport_error_propagates(self, full_env, mocker):
        client = make_client(mocker, full_env)
        import requests as real_requests
        mocker.patch.object(client.session, 'post', side_effect=real_requests.exceptions.ConnectionError())

        with pytest.raises(CosmosScriptError) as excinfo:
            client.halt_script(1)
        assert excinfo.value.script_id == 1


# ---------------------------------------------------------------------------
# monitor_script
# ---------------------------------------------------------------------------

class TestMonitorScript:
    def test_success_completion(self, full_env, mocker):
        client = make_client(mocker, full_env)
        mocker.patch.object(client, 'get_script', return_value={
            'found': True, 'running': False, 'state': 'completed', 'line_no': 10, 'script': {}
        })
        mocker.patch('ing_lib_cosmos.cosmos.time.sleep')

        results = list(client.monitor_script(1, timeout=5))
        result = results[-1]

        assert result['running'] is False
        assert result['state'] == 'completed'
        assert isinstance(result['timeout_remaining'], int)
        assert result['timeout_remaining'] >= 0

    def test_error_state(self, full_env, mocker):
        client = make_client(mocker, full_env)
        mocker.patch.object(client, 'get_script', return_value={
            'found': True, 'running': True, 'state': 'error', 'line_no': 3,
            'script': {'errors': ['boom']}
        })
        mocker.patch('ing_lib_cosmos.cosmos.time.sleep')

        result = list(client.monitor_script(1, timeout=5))[-1]

        assert result['running'] is False
        assert result['state'] == 'error'
        assert 'boom' in result['error']
        assert isinstance(result['timeout_remaining'], int)
        assert result['timeout_remaining'] >= 0

    def test_stopped_state(self, full_env, mocker):
        client = make_client(mocker, full_env)
        mocker.patch.object(client, 'get_script', return_value={
            'found': True, 'running': False, 'state': 'stopped', 'line_no': 3, 'script': {}
        })
        mocker.patch('ing_lib_cosmos.cosmos.time.sleep')

        result = list(client.monitor_script(1, timeout=5))[-1]

        assert result['running'] is False
        assert result['state'] == 'stopped'
        assert isinstance(result['timeout_remaining'], int)
        assert result['timeout_remaining'] >= 0

    def test_not_found_never_executed(self, full_env, mocker):
        client = make_client(mocker, full_env)
        mocker.patch.object(client, 'get_script', return_value={
            'found': False, 'running': False, 'state': None, 'line_no': 0, 'script': None
        })
        mocker.patch('ing_lib_cosmos.cosmos.time.sleep')

        result = list(client.monitor_script(1, timeout=5))[-1]

        assert result['state'] == 'not_found'
        assert 'never progressed' in result['error']
        assert isinstance(result['timeout_remaining'], int)
        assert result['timeout_remaining'] >= 0

    def test_not_found_after_executed(self, full_env, mocker):
        client = make_client(mocker, full_env)
        # First poll: running (marks script_executed True); second poll: not found
        mocker.patch.object(client, 'get_script', side_effect=[
            {'found': True, 'running': True, 'state': 'running', 'line_no': 5, 'script': {}},
            {'found': False, 'running': False, 'state': None, 'line_no': 0, 'script': None},
        ])
        mocker.patch('ing_lib_cosmos.cosmos.time.sleep')

        result = list(client.monitor_script(1, timeout=5))[-1]

        assert result['state'] == 'completed'
        assert result['running'] is False
        assert isinstance(result['timeout_remaining'], int)
        assert result['timeout_remaining'] >= 0

    def test_timeout(self, full_env, mocker):
        client = make_client(mocker, full_env)
        mocker.patch.object(client, 'get_script', return_value={
            'found': True, 'running': True, 'state': 'running', 'line_no': 1, 'script': {}
        })
        mocker.patch('ing_lib_cosmos.cosmos.time.sleep')
        mocker.patch.object(client, 'halt_script', return_value={'script_id': 1, 'running': False, 'already_stopped': False})

        # Simulate time passing beyond timeout using a controlled clock
        times = iter([0, 0, 10, 20])
        mocker.patch('ing_lib_cosmos.cosmos.time.time', side_effect=lambda: next(times, 20))

        with pytest.raises(CosmosScriptError):
            list(client.monitor_script(1, timeout=5, poll_interval=1))

    def test_timeout_remaining_is_streamed_to_terminal_result(self, full_env, mocker):
        client = make_client(mocker, full_env)
        mocker.patch.object(client, 'get_script', side_effect=[
            {'found': True, 'running': True, 'state': 'running', 'line_no': 1, 'script': {}},
            {'found': True, 'running': True, 'state': 'running', 'line_no': 2, 'script': {}},
            {'found': True, 'running': False, 'state': 'completed', 'line_no': 3, 'script': {}},
        ])
        mocker.patch('ing_lib_cosmos.cosmos.time.sleep')
        mocker.patch('ing_lib_cosmos.cosmos.time.time', side_effect=[100, 100, 102, 104])

        results = list(client.monitor_script(1, timeout=5))

        assert [result['timeout_remaining'] for result in results] == [5, 3, 1, 1]
        assert all(isinstance(result['timeout_remaining'], int) for result in results)

    def test_intermediate_statuses_streamed_before_terminal(self, full_env, mocker):
        client = make_client(mocker, full_env)
        mocker.patch.object(client, 'get_script', side_effect=[
            {'found': True, 'running': True, 'state': 'running', 'line_no': 1, 'script': {}},
            {'found': True, 'running': True, 'state': 'running', 'line_no': 2, 'script': {}},
            {'found': True, 'running': False, 'state': 'completed', 'line_no': 3, 'script': {}},
        ])
        mocker.patch('ing_lib_cosmos.cosmos.time.sleep')

        results = list(client.monitor_script(1, timeout=5))

        # Two intermediate raw statuses + one terminal result
        assert len(results) == 4
        assert results[0]['found'] is True
        assert 'state' in results[0]
        assert results[1]['found'] is True
        assert results[-1]['state'] == 'completed'
        assert results[-1]['running'] is False
