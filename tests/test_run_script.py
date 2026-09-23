"""
Tests for steps/run_script/run_script.py

`main()` reads/writes files via ing_lib.steps helpers and drives a
CosmosAPIClient; those dependencies are mocked here so tests focus on the
step's control flow and use of ing_lib_cosmos.cosmos's exception-based API.
"""
import copy

import pytest

import run_script
from ing_lib_cosmos.cosmos import CosmosRequestError, CosmosScriptError


def make_input(entries):
    return {'entries': entries}


def make_entry(script_name='TARGET/procedures/script.py', wait_for_completion=True, timeout=5):
    return {
        'entry_inputs': {
            'script_name': script_name,
            'wait_for_completion': wait_for_completion,
            'timeout': timeout,
        }
    }


@pytest.fixture(autouse=True)
def mock_io(mocker):
    """Mock the file I/O helpers used by main() so no real files are touched."""
    mocker.patch.object(run_script, 'get_input_output_paths',
                         return_value=('/tmp/in.json', '/tmp/out.json'))
    write_mock = mocker.patch.object(run_script, 'write_output_file')
    return write_mock


def set_input(mocker, entries):
    mocker.patch.object(run_script, 'read_input_file', return_value=make_input(entries))


def last_output_dict(write_mock):
    return write_mock.call_args_list[-1][0][0]


class TestMain:
    def test_client_init_failure_exits_with_error(self, mocker, mock_io):
        set_input(mocker, [])
        mocker.patch.object(run_script, 'CosmosAPIClient', side_effect=ValueError('bad config'))

        with pytest.raises(SystemExit) as excinfo:
            run_script.main()

        assert excinfo.value.code == -1
        assert last_output_dict(mock_io)['custom_script_status'] == 'ERROR'

    def test_start_script_failure_marks_entry_fail(self, mocker, mock_io):
        set_input(mocker, [make_entry()])
        client = mocker.Mock()
        client.start_script.side_effect = CosmosRequestError('boom', status_code=500)
        mocker.patch.object(run_script, 'CosmosAPIClient', return_value=client)

        run_script.main()

        output = last_output_dict(mock_io)
        entry = output['entries'][0]
        assert entry['verification_status'] == 'FAIL'
        assert entry['entry_outputs']['success'] is False
        assert output['custom_script_status'] == 'FAIL'

    def test_wait_for_completion_success_marks_pass(self, mocker, mock_io):
        set_input(mocker, [make_entry(wait_for_completion=True)])
        snapshots = []
        mock_io.side_effect = lambda output, _: snapshots.append(copy.deepcopy(output))
        client = mocker.Mock()
        client.start_script.return_value = {'script_id': 42, 'running': True}
        client.monitor_script.return_value = iter([
            {'found': True, 'running': True, 'state': 'running', 'line_no': 1,
             'timeout_remaining': 5, 'script': {}},
            {'script_id': 42, 'state': 'completed', 'running': False, 'timeout_remaining': 3},
        ])
        mocker.patch.object(run_script, 'CosmosAPIClient', return_value=client)

        run_script.main()

        output = last_output_dict(mock_io)
        entry = output['entries'][0]
        assert entry['verification_status'] == 'PASS'
        assert entry['entry_outputs']['timeout_remaining'] == 3
        assert output['custom_script_status'] == 'PASS'

        updates = [
            snapshot['entries'][0]['entry_outputs']['timeout_remaining']
            for snapshot in snapshots
            if snapshot['entries'][0]['entry_outputs'].get('timeout_remaining') is not None
        ]
        assert any(
            previous == 5 and current == 3
            for previous, current in zip(updates, updates[1:])
        )

    def test_wait_for_completion_failure_marks_fail(self, mocker, mock_io):
        set_input(mocker, [make_entry(wait_for_completion=True)])
        client = mocker.Mock()
        client.start_script.return_value = {'script_id': 42, 'running': True}
        client.monitor_script.return_value = iter([
            {'script_id': 42, 'state': 'error', 'running': False, 'error': 'script failed'},
        ])
        mocker.patch.object(run_script, 'CosmosAPIClient', return_value=client)

        run_script.main()

        output = last_output_dict(mock_io)
        entry = output['entries'][0]
        assert entry['verification_status'] == 'FAIL'
        assert output['custom_script_status'] == 'FAIL'

    def test_monitor_script_raises_marks_fail(self, mocker, mock_io):
        set_input(mocker, [make_entry(wait_for_completion=True)])
        client = mocker.Mock()
        client.start_script.return_value = {'script_id': 42, 'running': True}

        def raising_generator():
            raise CosmosScriptError('timed out', script_id=42)
            yield  # pragma: no cover - unreachable, makes this a generator function

        client.monitor_script.return_value = raising_generator()
        mocker.patch.object(run_script, 'CosmosAPIClient', return_value=client)

        run_script.main()

        output = last_output_dict(mock_io)
        entry = output['entries'][0]
        assert entry['verification_status'] == 'FAIL'
        assert 'timed out' in entry['entry_outputs']['error']

    def test_no_wait_found_marks_pass(self, mocker, mock_io):
        set_input(mocker, [make_entry(wait_for_completion=False)])
        client = mocker.Mock()
        client.start_script.return_value = {'script_id': 7, 'running': True}
        client.get_script.return_value = {
            'found': True, 'running': True, 'state': 'running', 'line_no': 1, 'script': {}
        }
        mocker.patch.object(run_script, 'CosmosAPIClient', return_value=client)

        run_script.main()

        output = last_output_dict(mock_io)
        entry = output['entries'][0]
        assert entry['verification_status'] == 'PASS'
        assert entry['entry_outputs']['timeout_remaining'] == 5
        assert output['custom_script_status'] == 'PASS'

    def test_no_wait_not_found_marks_fail(self, mocker, mock_io):
        set_input(mocker, [make_entry(wait_for_completion=False)])
        client = mocker.Mock()
        client.start_script.return_value = {'script_id': 7, 'running': True}
        client.get_script.return_value = {
            'found': False, 'running': False, 'state': None, 'line_no': 0, 'script': None
        }
        mocker.patch.object(run_script, 'CosmosAPIClient', return_value=client)

        run_script.main()

        output = last_output_dict(mock_io)
        entry = output['entries'][0]
        assert entry['verification_status'] == 'FAIL'
        assert entry['entry_outputs']['error'] == 'Script not found'
        assert output['custom_script_status'] == 'FAIL'

    def test_multiple_entries_mixed_results(self, mocker, mock_io):
        set_input(mocker, [
            make_entry(script_name='a.py', wait_for_completion=True),
            make_entry(script_name='b.py', wait_for_completion=True),
        ])
        client = mocker.Mock()
        client.start_script.side_effect = [
            {'script_id': 1, 'running': True},
            {'script_id': 2, 'running': True},
        ]
        client.monitor_script.side_effect = [
            iter([{'script_id': 1, 'state': 'completed', 'running': False}]),
            iter([{'script_id': 2, 'state': 'error', 'running': False, 'error': 'oops'}]),
        ]
        mocker.patch.object(run_script, 'CosmosAPIClient', return_value=client)

        run_script.main()

        output = last_output_dict(mock_io)
        assert output['entries'][0]['verification_status'] == 'PASS'
        assert output['entries'][1]['verification_status'] == 'FAIL'
        assert output['custom_script_status'] == 'FAIL'
