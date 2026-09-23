"""
Tests for steps/halt_all_scripts/halt_all_scripts.py

`main()` reads/writes files via ing_lib.steps helpers and drives a
CosmosAPIClient; those dependencies are mocked here so tests focus on the
step's control flow and use of ing_lib_cosmos.cosmos's exception-based API.
"""
import pytest

import halt_all_scripts
from ing_lib_cosmos.cosmos import IngeniumCosmosError, CosmosRequestError


@pytest.fixture(autouse=True)
def mock_io(mocker):
    """Mock the file I/O helpers used by main() so no real files are touched."""
    mocker.patch.object(halt_all_scripts, 'get_input_output_paths',
                         return_value=('/tmp/in.json', '/tmp/out.json'))
    mocker.patch.object(halt_all_scripts, 'read_input_file', return_value={})
    write_mock = mocker.patch.object(halt_all_scripts, 'write_output_file')
    return write_mock


def last_output_dict(write_mock):
    return write_mock.call_args_list[-1][0][0]


class TestMain:
    def test_client_init_failure_exits_with_error(self, mocker, mock_io):
        mocker.patch.object(halt_all_scripts, 'CosmosAPIClient', side_effect=ValueError('bad config'))

        with pytest.raises(SystemExit) as excinfo:
            halt_all_scripts.main()

        assert excinfo.value.code == -1
        assert last_output_dict(mock_io)['custom_script_status'] == 'ERROR'

    def test_get_all_scripts_failure_exits_with_error(self, mocker, mock_io):
        client = mocker.Mock()
        client.get_all_scripts.side_effect = CosmosRequestError('boom', status_code=500)
        mocker.patch.object(halt_all_scripts, 'CosmosAPIClient', return_value=client)

        with pytest.raises(SystemExit) as excinfo:
            halt_all_scripts.main()

        assert excinfo.value.code == -1
        assert last_output_dict(mock_io)['custom_script_status'] == 'ERROR'

    def test_missing_running_scripts_key_exits_with_error(self, mocker, mock_io):
        client = mocker.Mock()
        client.get_all_scripts.return_value = {}  # no 'running_scripts' key
        mocker.patch.object(halt_all_scripts, 'CosmosAPIClient', return_value=client)

        with pytest.raises(SystemExit) as excinfo:
            halt_all_scripts.main()

        assert excinfo.value.code == -1
        assert last_output_dict(mock_io)['custom_script_status'] == 'ERROR'

    def test_no_running_scripts_passes(self, mocker, mock_io):
        client = mocker.Mock()
        client.get_all_scripts.return_value = {'running_scripts': []}
        mocker.patch.object(halt_all_scripts, 'CosmosAPIClient', return_value=client)

        halt_all_scripts.main()

        output = last_output_dict(mock_io)
        assert output['custom_script_status'] == 'PASS'
        assert output['output']['scripts_running'] == 0
        assert output['output']['scripts_halted'] == 0
        client.halt_script.assert_not_called()

    def test_halts_all_running_scripts_passes(self, mocker, mock_io):
        client = mocker.Mock()
        client.get_all_scripts.return_value = {
            'running_scripts': [
                {'name': 1, 'state': 'running', 'filename': 'a.py', 'line_no': 3,
                 'start_time': 't0', 'updated_at': 't1'},
                {'name': 2, 'state': 'running', 'filename': 'b.py', 'line_no': 5,
                 'start_time': 't0', 'updated_at': 't1'},
            ]
        }
        client.halt_script.side_effect = [
            {'script_id': 1, 'running': False, 'already_stopped': False},
            {'script_id': 2, 'running': False, 'already_stopped': False},
        ]
        mocker.patch.object(halt_all_scripts, 'CosmosAPIClient', return_value=client)

        halt_all_scripts.main()

        output = last_output_dict(mock_io)
        assert output['custom_script_status'] == 'PASS'
        assert output['output']['scripts_running'] == 2
        assert output['output']['scripts_halted'] == 2
        assert client.halt_script.call_count == 2

    def test_halt_failure_marks_overall_fail(self, mocker, mock_io):
        client = mocker.Mock()
        client.get_all_scripts.return_value = {
            'running_scripts': [
                {'name': 1, 'state': 'running', 'filename': 'a.py', 'line_no': 3,
                 'start_time': 't0', 'updated_at': 't1'},
            ]
        }
        client.halt_script.side_effect = CosmosRequestError('failed to halt', status_code=500)
        mocker.patch.object(halt_all_scripts, 'CosmosAPIClient', return_value=client)

        halt_all_scripts.main()

        output = last_output_dict(mock_io)
        assert output['custom_script_status'] == 'FAIL'
        assert output['output']['scripts_halted'] == 0
        assert output['output_array'][0]['script_halted'] is False
