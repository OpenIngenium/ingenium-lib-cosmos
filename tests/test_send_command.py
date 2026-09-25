"""
Tests for steps/send_command/send_command.py

`main()` reads/writes files via ing_lib.steps helpers and drives a
CosmosAPIClient; those dependencies are mocked here so tests focus on the
step's control flow and use of ing_lib_cosmos.cosmos's exception-based API.
"""
import pytest

import send_command
from ing_lib_cosmos.cosmos import CosmosRequestError


def make_input(entries):
    return {'entries': entries}


def make_entry(command_string='TGT__CMD with PARAM1 5', cmd_check='ENABLED'):
    return {
        'entry_inputs': {
            'command_string': command_string,
            'cmd_check': cmd_check,
        },
    }


@pytest.fixture(autouse=True)
def mock_io(mocker):
    """Mock the file I/O helpers used by main() so no real files are touched."""
    mocker.patch.object(send_command, 'get_input_output_paths',
                         return_value=('/tmp/in.json', '/tmp/out.json'))
    write_mock = mocker.patch.object(send_command, 'write_output_file')
    return write_mock


def set_input(mocker, entries):
    mocker.patch.object(send_command, 'read_input_file', return_value=make_input(entries))


def last_output_dict(write_mock):
    return write_mock.call_args_list[-1][0][0]


class TestMain:
    def test_client_init_failure_exits_with_error(self, mocker, mock_io):
        set_input(mocker, [])
        mocker.patch.object(send_command, 'CosmosAPIClient', side_effect=ValueError('bad config'))

        with pytest.raises(SystemExit) as excinfo:
            send_command.main()

        assert excinfo.value.code == -1
        assert last_output_dict(mock_io)['custom_script_status'] == 'ERROR'

    def test_parses_command_string(self, mocker, mock_io):
        set_input(mocker, [make_entry(command_string='TGT__CMD with PARAM1 5')])
        client = mocker.Mock()
        client.send_command.return_value = 'ok'
        client.get_cmd_cnt.return_value = {'count': 3}
        client.get_cmd_time.return_value = {'time': 1_700_000_000.0}
        mocker.patch.object(send_command, 'CosmosAPIClient', return_value=client)

        send_command.main()

        client.send_command.assert_called_once_with('TGT CMD with PARAM1 5', 'ENABLED')
        client.get_cmd_cnt.assert_called_once_with('TGT', 'CMD')
        client.get_cmd_time.assert_called_once_with('TGT', 'CMD')

    def test_success_path_sets_status_and_outputs(self, mocker, mock_io):
        set_input(mocker, [make_entry()])
        client = mocker.Mock()
        client.send_command.return_value = 'ok'
        client.get_cmd_cnt.return_value = {'count': 7}
        client.get_cmd_time.return_value = {'time': 1_700_000_000.0}
        mocker.patch.object(send_command, 'CosmosAPIClient', return_value=client)

        send_command.main()

        output = last_output_dict(mock_io)
        entry = output['entries'][0]
        assert entry['entry_outputs']['cmd_status'] == 'success'
        assert entry['verification_status'] == 'PASS'
        assert entry['entry_outputs']['cmd_cnt'] == 7
        assert entry['entry_outputs']['timestamp'].endswith('Z')
        assert output['custom_script_status'] == 'PASS'

    def test_send_command_failure_sets_fail_status(self, mocker, mock_io):
        """
        When client.send_command() raises (the real client raises
        IngeniumCosmosError subclasses on failure rather than returning a
        'success' flag), the entry should be marked failed without calling
        the cmd_cnt/cmd_time follow-ups.
        """
        set_input(mocker, [make_entry()])
        client = mocker.Mock()
        client.send_command.side_effect = CosmosRequestError('boom', status_code=500)
        mocker.patch.object(send_command, 'CosmosAPIClient', return_value=client)

        send_command.main()

        output = last_output_dict(mock_io)
        entry = output['entries'][0]
        assert entry['entry_outputs']['cmd_status'] == 'fail'
        assert entry['verification_status'] == 'FAIL'
        assert entry['entry_outputs']['cmd_cnt'] == 0
        assert entry['entry_outputs']['timestamp'] == 'N/A'
        assert output['custom_script_status'] == 'FAIL'
        client.get_cmd_cnt.assert_not_called()
        client.get_cmd_time.assert_not_called()

    def test_cmd_cnt_failure_marks_entry_fail(self, mocker, mock_io):
        """
        get_cmd_cnt() raising should also be caught and treated as a failed
        entry, since it's covered by the same try/except as send_command.
        """
        set_input(mocker, [make_entry()])
        client = mocker.Mock()
        client.send_command.return_value = 'ok'
        client.get_cmd_cnt.side_effect = CosmosRequestError('err', status_code=500)
        client.get_cmd_time.return_value = {'time': 1_700_000_000.0}
        mocker.patch.object(send_command, 'CosmosAPIClient', return_value=client)

        send_command.main()

        output = last_output_dict(mock_io)
        entry = output['entries'][0]
        assert entry['entry_outputs']['cmd_status'] == 'fail'
        assert entry['verification_status'] == 'FAIL'
        assert output['custom_script_status'] == 'FAIL'

    def test_cmd_time_none_sets_na_timestamp(self, mocker, mock_io):
        """
        When get_cmd_time succeeds but returns time=None (e.g. no matching
        command has ever been sent), the timestamp output should fall back
        to 'N/A' rather than erroring.
        """
        set_input(mocker, [make_entry()])
        client = mocker.Mock()
        client.send_command.return_value = 'ok'
        client.get_cmd_cnt.return_value = {'count': 0}
        client.get_cmd_time.return_value = {'time': None}
        mocker.patch.object(send_command, 'CosmosAPIClient', return_value=client)

        send_command.main()

        output = last_output_dict(mock_io)
        entry = output['entries'][0]
        assert entry['entry_outputs']['cmd_status'] == 'success'
        assert entry['entry_outputs']['timestamp'] == 'N/A'

    def test_malformed_command_string_marks_entry_fail(self, mocker, mock_io):
        set_input(mocker, [make_entry(command_string='TGT CMD with PARAM1 5')])
        client = mocker.Mock()
        mocker.patch.object(send_command, 'CosmosAPIClient', return_value=client)

        send_command.main()

        output = last_output_dict(mock_io)
        entry = output['entries'][0]
        assert entry['verification_status'] == 'FAIL'
        assert entry['entry_outputs']['cmd_status'] == 'fail'
        client.send_command.assert_not_called()

    def test_no_entries_status_pass(self, mocker, mock_io):
        """With no entries, the loop body never runs and overall status
        defaults to PASS."""
        set_input(mocker, [])
        client = mocker.Mock()
        mocker.patch.object(send_command, 'CosmosAPIClient', return_value=client)

        send_command.main()

        output = last_output_dict(mock_io)
        assert output['entries'] == []
        assert output['custom_script_status'] == 'PASS'
        client.send_command.assert_not_called()

    def test_cmd_time_failure_marks_entry_fail(self, mocker, mock_io):
        """
        get_cmd_time() raising should also be caught by the same try/except
        as send_command/get_cmd_cnt, marking the entry failed.
        """
        set_input(mocker, [make_entry()])
        client = mocker.Mock()
        client.send_command.return_value = 'ok'
        client.get_cmd_cnt.return_value = {'count': 1}
        client.get_cmd_time.side_effect = CosmosRequestError('err', status_code=500)
        mocker.patch.object(send_command, 'CosmosAPIClient', return_value=client)

        send_command.main()

        output = last_output_dict(mock_io)
        entry = output['entries'][0]
        assert entry['entry_outputs']['cmd_status'] == 'fail'
        assert entry['verification_status'] == 'FAIL'
        assert entry['entry_outputs']['cmd_cnt'] == 0
        assert entry['entry_outputs']['timestamp'] == 'N/A'
        assert output['custom_script_status'] == 'FAIL'

    def test_output_summary_lists_each_command(self, mocker, mock_io):
        set_input(mocker, [
            make_entry(command_string='TGT__CMD1 with PARAM1 5'),
            make_entry(command_string='TGT__CMD2 with PARAM1 6'),
        ])
        client = mocker.Mock()
        client.send_command.return_value = 'ok'
        client.get_cmd_cnt.return_value = {'count': 1}
        client.get_cmd_time.return_value = {'time': 1_700_000_000.0}
        mocker.patch.object(send_command, 'CosmosAPIClient', return_value=client)

        send_command.main()

        output = last_output_dict(mock_io)
        summary = output['output_summary']
        assert 'TGT__CMD1 with PARAM1 5' in summary
        assert 'TGT__CMD2 with PARAM1 6' in summary
        assert summary.count('status of PASS') == 2

    def test_write_output_file_called_per_entry_plus_init(self, mocker, mock_io):
        """write_output_file should be called once to initialize output and
        once more per processed entry."""
        set_input(mocker, [make_entry(), make_entry()])
        client = mocker.Mock()
        client.send_command.return_value = 'ok'
        client.get_cmd_cnt.return_value = {'count': 1}
        client.get_cmd_time.return_value = {'time': 1_700_000_000.0}
        mocker.patch.object(send_command, 'CosmosAPIClient', return_value=client)

        send_command.main()

        # 1 init write + 2 per-entry writes + 1 final status write = 4
        assert mock_io.call_count == 4

    def test_multiple_entries_mixed_results(self, mocker, mock_io):
        set_input(mocker, [
            make_entry(command_string='TGT__CMD1 with PARAM1 5'),
            make_entry(command_string='TGT__CMD2 with PARAM1 5'),
        ])
        client = mocker.Mock()
        client.send_command.side_effect = ['ok', CosmosRequestError('boom', status_code=500)]
        client.get_cmd_cnt.return_value = {'count': 1}
        client.get_cmd_time.return_value = {'time': 1_700_000_000.0}
        mocker.patch.object(send_command, 'CosmosAPIClient', return_value=client)

        send_command.main()

        output = last_output_dict(mock_io)
        assert output['entries'][0]['verification_status'] == 'PASS'
        assert output['entries'][1]['verification_status'] == 'FAIL'
        assert output['custom_script_status'] == 'FAIL'
