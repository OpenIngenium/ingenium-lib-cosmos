"""
Tests for steps/query_telem/query_telem.py

`main()` reads/writes files via ing_lib.steps helpers and drives a
CosmosAPIClient; those dependencies (plus `verify_wait_telemetry` itself) are
mocked here so tests focus on query_telem.py's own control flow: building the
combined query list/telemetry_query_func from all entries, issuing a single
verify_wait_telemetry call sourcing start_time/timeout/lookback from the
top-level 'inputs' block, sourcing prior_value for CHANGE checks, and mapping
verify_wait_telemetry's predict_results onto entry_outputs.
"""
import pytest

import query_telem
from ing_lib.steps import InputError
from ing_lib_cosmos.cosmos import IngeniumCosmosError


def make_input(entries, states=None, inputs=None):
    input_dict = {'config': {}, 'entries': entries}
    if states is not None:
        input_dict['states'] = states
    if inputs is not None:
        input_dict['inputs'] = inputs
    return input_dict


def make_entry(telem_name='TESTPKT__GENERIC/CHANNEL_ONE__FIELD_A',
               verify_wait='VERIFY', verify_on='VALUE', dn_eu='EU',
               verification_condition='EQUAL,INHIBIT,,',
               bit_mask=None, bit_op='NONE'):
    if ',' not in telem_name:
        telem_name = f'1234,{telem_name}'

    return {
        'entry_inputs': {
            'telem_name': telem_name,
            'verify_wait': verify_wait,
            'verify_on': verify_on,
            'dn_eu': dn_eu,
            'verification_condition': verification_condition,
            'bit_mask': bit_mask,
            'bit_op': bit_op,
        },
    }


def make_predict_result(telem_name, actual_value='INHIBIT', verification_status='PASS',
                         telem_time='2026-01-01T00:00:00Z'):
    return {
        'telem_uuid': telem_name,
        'predict': {},
        'actual_value': actual_value,
        'verification_status': verification_status,
        'data_present': True,
        'telem_details': {'time': telem_time},
    }


def make_results(predict_results):
    """
    Build a single verify_wait_telemetry-shaped snapshot from a list of
    predict_results (in the same order as the entries/query).
    """
    query_matches_predict = all(
        r['verification_status'] == 'PASS' for r in predict_results
    )
    return {
        'query_matches_predict': query_matches_predict,
        'predict_results': predict_results,
        'telemetry': {},
        'query_complete': True,
        'query_timeout': False,
        'predict_complete': [True] * len(predict_results),
    }


def find_predict(query_arg, telem_name):
    return next(p for p in query_arg if p['telem_uuid'] == telem_name)


@pytest.mark.parametrize(
    'verification_condition, expected',
    [
        ('GREATER_THAN,3,,', '199 > 3'),
        ('GREATER_THAN_OR_EQUAL,3,,', '199 >= 3'),
        ('LESS_THAN,3,,', '199 < 3'),
        ('LESS_THAN_OR_EQUAL,3,,', '199 <= 3'),
        ('EQUAL,3,,', '199 == 3'),
        ('NOT_EQUAL,3,,', '199 != 3'),
        ('CONTAINS,READY,,', 'READY contained in 199'),
        ('INCLUSIVE_RANGE,,3,7', '3 <= 199 <= 7'),
        ('EXCLUSIVE_RANGE,,3,7', '3 < 199 < 7'),
        ('RECORD,,,', 'Record 199'),
        ('NOT_PRESENT,,,', 'telemetry is not present'),
    ],
)
def test_format_verification_condition(verification_condition, expected):
    assert query_telem.format_verification_condition(verification_condition, 199) == expected


def test_apply_telem_result_populates_telem_eval():
    entry = make_entry(verification_condition='GREATER_THAN,3,,')
    entry['entry_outputs'] = {
        'actual_value': '',
        'telem_time': '',
        'telem_eval': '',
    }

    query_telem.apply_telem_result_to_entry(
        entry,
        make_predict_result('TESTPKT__GENERIC/CHANNEL_ONE__FIELD_A', actual_value=4),
    )

    assert entry['entry_outputs']['telem_eval'] == '4 > 3'


@pytest.fixture(autouse=True)
def mock_io(mocker):
    """Mock the file I/O helpers used by main() so no real files are touched."""
    mocker.patch.object(query_telem, 'get_input_output_paths',
                         return_value=('/tmp/in.json', '/tmp/out.json'))
    write_mock = mocker.patch.object(query_telem, 'write_output_file')
    return write_mock


def set_input(mocker, entries, states=None, inputs=None):
    mocker.patch.object(query_telem, 'read_input_file', return_value=make_input(entries, states, inputs))


def last_output_dict(write_mock):
    return write_mock.call_args_list[-1][0][0]


class TestMain:
    def test_build_combined_query_uses_canonical_name_for_uuid_and_entry_map(self):
        telem_name = 'TESTPKT__GENERIC/CHANNEL_ONE__FIELD_A'
        entries = [make_entry(telem_name=f'1234,{telem_name}')]

        query, entry_map = query_telem.build_combined_query(make_input(entries), entries)

        assert query[0]['telem_uuid'] == telem_name
        assert entry_map == {telem_name: 'EU'}

    def test_client_init_failure_exits_with_error(self, mocker, mock_io):
        set_input(mocker, [])
        mocker.patch.object(query_telem, 'CosmosAPIClient', side_effect=ValueError('bad config'))

        with pytest.raises(SystemExit) as excinfo:
            query_telem.main()

        assert excinfo.value.code == -1
        assert last_output_dict(mock_io)['custom_script_status'] == 'ERROR'

    def test_no_entries_status_pass(self, mocker, mock_io):
        set_input(mocker, [])
        mocker.patch.object(query_telem, 'CosmosAPIClient', return_value=mocker.Mock())

        query_telem.main()

        output = last_output_dict(mock_io)
        assert output['entries'] == []
        assert output['custom_script_status'] == 'PASS'

    def test_pass_case_sets_status_and_outputs(self, mocker, mock_io):
        telem_name = 'TESTPKT__GENERIC/CHANNEL_ONE__FIELD_A'
        set_input(mocker, [make_entry(telem_name=telem_name)])
        mocker.patch.object(query_telem, 'CosmosAPIClient', return_value=mocker.Mock())
        mocker.patch.object(
            query_telem, 'verify_wait_telemetry',
            return_value=[make_results([
                make_predict_result(telem_name, actual_value='INHIBIT', verification_status='PASS'),
            ])],
        )

        query_telem.main()

        output = last_output_dict(mock_io)
        entry = output['entries'][0]
        assert entry['verification_status'] == 'PASS'
        assert entry['entry_outputs']['actual_value'] == 'INHIBIT'
        assert entry['entry_outputs']['telem_time'] == '2026-01-01T00:00:00Z'
        assert entry['entry_outputs']['telem_eval'] == 'INHIBIT == INHIBIT'
        assert output['custom_script_status'] == 'PASS'

    def test_fail_case_sets_fail_status(self, mocker, mock_io):
        telem_name = 'TESTPKT__GENERIC/CHANNEL_ONE__FIELD_A'
        set_input(mocker, [make_entry(telem_name=telem_name)])
        mocker.patch.object(query_telem, 'CosmosAPIClient', return_value=mocker.Mock())
        mocker.patch.object(
            query_telem, 'verify_wait_telemetry',
            return_value=[make_results([
                make_predict_result(telem_name, actual_value='ENABLED', verification_status='FAIL'),
            ])],
        )

        query_telem.main()

        output = last_output_dict(mock_io)
        entry = output['entries'][0]
        assert entry['verification_status'] == 'FAIL'
        assert output['custom_script_status'] == 'FAIL'

    def test_input_error_marks_entries_error_with_schema_preserved(self, mocker, mock_io):
        telem_a = 'TESTPKT__GENERIC/CHANNEL_THREE__FIELD_C'
        telem_b = 'TESTPKT__GENERIC/CHANNEL_FOUR__FIELD_D'
        set_input(mocker, [make_entry(telem_name=telem_a), make_entry(telem_name=telem_b)])
        mocker.patch.object(query_telem, 'CosmosAPIClient', return_value=mocker.Mock())
        mocker.patch.object(query_telem, 'verify_wait_telemetry',
                             side_effect=InputError('bad query'))

        with pytest.raises(SystemExit) as excinfo:
            query_telem.main()

        assert excinfo.value.code == -1
        output = last_output_dict(mock_io)
        assert output['entries'][0]['verification_status'] == 'ERROR'
        assert output['entries'][1]['verification_status'] == 'ERROR'
        assert set(output['entries'][0]['entry_outputs']) == {'actual_value', 'telem_time', 'telem_eval'}
        assert 'bad query' in output['output_summary']
        assert output['custom_script_status'] == 'ERROR'

    def test_conflicting_dn_eu_for_same_telem_name_marks_entries_error(self, mocker, mock_io):
        telem_name = 'TESTPKT__GENERIC/CHANNEL_ONE__FIELD_A'
        set_input(mocker, [
            make_entry(telem_name=telem_name, dn_eu='DN'),
            make_entry(telem_name=telem_name, dn_eu='EU'),
        ])
        mocker.patch.object(query_telem, 'CosmosAPIClient', return_value=mocker.Mock())
        verify_mock = mocker.patch.object(query_telem, 'verify_wait_telemetry')

        with pytest.raises(SystemExit) as excinfo:
            query_telem.main()

        assert excinfo.value.code == -1
        output = last_output_dict(mock_io)
        assert output['entries'][0]['verification_status'] == 'ERROR'
        assert output['entries'][1]['verification_status'] == 'ERROR'
        assert output['custom_script_status'] == 'ERROR'
        # Should fail fast, before ever issuing the telemetry query
        verify_mock.assert_not_called()

    def test_invalid_dn_eu_marks_entries_error(self, mocker, mock_io):
        telem_name = 'TESTPKT__GENERIC/CHANNEL_ONE__FIELD_A'
        set_input(mocker, [make_entry(telem_name=telem_name, dn_eu='BOGUS')])
        mocker.patch.object(query_telem, 'CosmosAPIClient', return_value=mocker.Mock())
        verify_mock = mocker.patch.object(query_telem, 'verify_wait_telemetry')

        with pytest.raises(SystemExit) as excinfo:
            query_telem.main()

        assert excinfo.value.code == -1
        output = last_output_dict(mock_io)
        assert output['entries'][0]['verification_status'] == 'ERROR'
        assert output['custom_script_status'] == 'ERROR'
        verify_mock.assert_not_called()

    def test_cosmos_error_marks_entries_error_with_schema_preserved(self, mocker, mock_io):
        telem_name = 'TESTPKT__GENERIC/CHANNEL_ONE__FIELD_A'
        set_input(mocker, [make_entry(telem_name=telem_name)])
        mocker.patch.object(query_telem, 'CosmosAPIClient', return_value=mocker.Mock())
        mocker.patch.object(query_telem, 'verify_wait_telemetry',
                             side_effect=IngeniumCosmosError('cosmos down'))

        with pytest.raises(SystemExit) as excinfo:
            query_telem.main()

        assert excinfo.value.code == -1
        output = last_output_dict(mock_io)
        entry = output['entries'][0]
        assert entry['verification_status'] == 'ERROR'
        assert set(entry['entry_outputs']) == {'actual_value', 'telem_time', 'telem_eval'}
        assert 'cosmos down' in output['output_summary']
        assert output['custom_script_status'] == 'ERROR'

    def test_verify_on_change_passes_prior_value_from_states(self, mocker, mock_io):
        telem_name = 'TESTPKT__GENERIC/CHANNEL_TWO__FIELD_B'
        states = {'variables': {'channel_variables': {telem_name: 4}}}
        set_input(mocker, [make_entry(telem_name=telem_name, verify_on='CHANGE')], states=states)
        mocker.patch.object(query_telem, 'CosmosAPIClient', return_value=mocker.Mock())
        verify_mock = mocker.patch.object(
            query_telem, 'verify_wait_telemetry',
            return_value=[make_results([
                make_predict_result(telem_name, actual_value=1, verification_status='PASS'),
            ])],
        )

        query_telem.main()

        query_arg = verify_mock.call_args[0][0]
        assert find_predict(query_arg, telem_name)['prior_value'] == 4

    def test_verify_on_change_missing_channel_defaults_to_none(self, mocker, mock_io):
        telem_name = 'TESTPKT__GENERIC/CHANNEL_TWO__FIELD_B'
        states = {'variables': {'channel_variables': {}}}
        set_input(mocker, [make_entry(telem_name=telem_name, verify_on='CHANGE')], states=states)
        mocker.patch.object(query_telem, 'CosmosAPIClient', return_value=mocker.Mock())
        verify_mock = mocker.patch.object(
            query_telem, 'verify_wait_telemetry',
            return_value=[make_results([
                make_predict_result(telem_name, actual_value=5, verification_status='FAIL'),
            ])],
        )

        query_telem.main()

        query_arg = verify_mock.call_args[0][0]
        assert 'prior_value' not in find_predict(query_arg, telem_name)

    def test_verify_on_value_ignores_states_channel_variables(self, mocker, mock_io):
        telem_name = 'TESTPKT__GENERIC/CHANNEL_ONE__FIELD_A'
        states = {'variables': {'channel_variables': {telem_name: 999}}}
        set_input(mocker, [make_entry(telem_name=telem_name, verify_on='VALUE')], states=states)
        mocker.patch.object(query_telem, 'CosmosAPIClient', return_value=mocker.Mock())
        verify_mock = mocker.patch.object(
            query_telem, 'verify_wait_telemetry',
            return_value=[make_results([
                make_predict_result(telem_name, actual_value='INHIBIT', verification_status='PASS'),
            ])],
        )

        query_telem.main()

        query_arg = verify_mock.call_args[0][0]
        assert 'prior_value' not in find_predict(query_arg, telem_name)

    def test_single_call_combines_all_entries(self, mocker, mock_io):
        telem_a = 'TESTPKT__GENERIC/CHANNEL_THREE__FIELD_C'
        telem_b = 'TESTPKT__GENERIC/CHANNEL_FOUR__FIELD_D'
        set_input(mocker, [make_entry(telem_name=telem_a), make_entry(telem_name=telem_b)])
        mocker.patch.object(query_telem, 'CosmosAPIClient', return_value=mocker.Mock())
        verify_mock = mocker.patch.object(
            query_telem, 'verify_wait_telemetry',
            return_value=[make_results([
                make_predict_result(telem_a, actual_value='INHIBIT', verification_status='PASS'),
                make_predict_result(telem_b, actual_value='INHIBIT', verification_status='PASS'),
            ])],
        )

        query_telem.main()

        assert verify_mock.call_count == 1
        query_arg = verify_mock.call_args[0][0]
        assert {p['telem_uuid'] for p in query_arg} == {telem_a, telem_b}
        assert all(p['verification_condition'] == 'EQUAL' for p in query_arg)
        assert all(p['verification_values'] == ['INHIBIT'] for p in query_arg)

    def test_csv_telem_name_is_normalized_for_query_and_change_lookup(self, mocker, mock_io):
        telem_name = 'TESTPKT__GENERIC/CHANNEL_ONE__FIELD_A'
        telem_id = '1234'
        set_input(
            mocker,
            [make_entry(telem_name=f'{telem_id},{telem_name}', verify_on='CHANGE')],
            states={'variables': {'channel_variables': {telem_name: 4}}},
        )
        mocker.patch.object(query_telem, 'CosmosAPIClient', return_value=mocker.Mock())
        verify_mock = mocker.patch.object(
            query_telem, 'verify_wait_telemetry',
            return_value=[make_results([
                make_predict_result(telem_name, actual_value=1, verification_status='PASS'),
            ])],
        )

        query_telem.main()

        query_arg = verify_mock.call_args[0][0]
        assert query_arg[0]['telem_uuid'] == telem_name
        assert query_arg[0]['prior_value'] == 4
        output = last_output_dict(mock_io)
        assert output['entries'][0]['entry_inputs']['telem_name'] == f'{telem_id},{telem_name}'
        assert telem_name in output['output_summary']
        assert f'{telem_name},{telem_id}' not in output['output_summary']

    def test_leading_csv_delimiter_is_normalized_for_query(self, mocker, mock_io):
        telem_name = 'MIRA__PRIM/DEFAULTBEACON__META.PID'
        set_input(mocker, [make_entry(telem_name=f',{telem_name}')])
        mocker.patch.object(query_telem, 'CosmosAPIClient', return_value=mocker.Mock())
        verify_mock = mocker.patch.object(
            query_telem, 'verify_wait_telemetry',
            return_value=[make_results([
                make_predict_result(telem_name, actual_value=1, verification_status='PASS'),
            ])],
        )

        query_telem.main()

        assert verify_mock.call_args[0][0][0]['telem_uuid'] == telem_name
        assert last_output_dict(mock_io)['custom_script_status'] == 'PASS'

    def test_empty_csv_telem_name_marks_entry_error(self, mocker, mock_io):
        set_input(mocker, [make_entry(telem_name=',')])
        mocker.patch.object(query_telem, 'CosmosAPIClient', return_value=mocker.Mock())
        verify_mock = mocker.patch.object(query_telem, 'verify_wait_telemetry')

        with pytest.raises(SystemExit) as excinfo:
            query_telem.main()

        assert excinfo.value.code == -1
        verify_mock.assert_not_called()
        output = last_output_dict(mock_io)
        assert output['entries'][0]['verification_status'] == 'ERROR'
        assert output['custom_script_status'] == 'ERROR'

    def test_timeout_lookback_start_time_sourced_from_top_level_inputs(self, mocker, mock_io):
        telem_name = 'TESTPKT__GENERIC/CHANNEL_ONE__FIELD_A'
        set_input(mocker, [make_entry(telem_name=telem_name)],
                  inputs={'start_time': None, 'timeout': 15, 'lookback': 10})
        mocker.patch.object(query_telem, 'CosmosAPIClient', return_value=mocker.Mock())
        verify_mock = mocker.patch.object(
            query_telem, 'verify_wait_telemetry',
            return_value=[make_results([
                make_predict_result(telem_name, actual_value='INHIBIT', verification_status='PASS'),
            ])],
        )

        query_telem.main()

        assert verify_mock.call_args.kwargs['timeout'] == 15
        assert verify_mock.call_args.kwargs['lookback'] == 10
        assert verify_mock.call_args.kwargs['start_time'] is None

    def test_start_time_doy_string_is_parsed(self, mocker, mock_io):
        telem_name = 'TESTPKT__GENERIC/CHANNEL_ONE__FIELD_A'
        set_input(mocker, [make_entry(telem_name=telem_name)],
                  inputs={'start_time': '2026-266T21:24:01', 'timeout': 60, 'lookback': 0})
        mocker.patch.object(query_telem, 'CosmosAPIClient', return_value=mocker.Mock())
        verify_mock = mocker.patch.object(
            query_telem, 'verify_wait_telemetry',
            return_value=[make_results([
                make_predict_result(telem_name, actual_value='INHIBIT', verification_status='PASS'),
            ])],
        )

        query_telem.main()

        start_time = verify_mock.call_args.kwargs['start_time']
        assert start_time is not None
        assert start_time.year == 2026
        assert start_time.timetuple().tm_yday == 266
        assert start_time.hour == 21
        assert start_time.minute == 24
        assert start_time.second == 1
        assert start_time.tzinfo is not None

    def test_invalid_start_time_marks_entries_error(self, mocker, mock_io):
        telem_name = 'TESTPKT__GENERIC/CHANNEL_ONE__FIELD_A'
        set_input(mocker, [make_entry(telem_name=telem_name)],
                  inputs={'start_time': 'not-a-date', 'timeout': 60, 'lookback': 0})
        mocker.patch.object(query_telem, 'CosmosAPIClient', return_value=mocker.Mock())
        verify_mock = mocker.patch.object(query_telem, 'verify_wait_telemetry')

        with pytest.raises(SystemExit) as excinfo:
            query_telem.main()

        assert excinfo.value.code == -1
        verify_mock.assert_not_called()
        output = last_output_dict(mock_io)
        assert output['entries'][0]['verification_status'] == 'ERROR'
        assert output['custom_script_status'] == 'ERROR'

    def test_output_summary_lists_each_channel(self, mocker, mock_io):
        telem_a = 'TESTPKT__GENERIC/CHANNEL_THREE__FIELD_C'
        telem_b = 'TESTPKT__GENERIC/CHANNEL_FOUR__FIELD_D'
        set_input(mocker, [make_entry(telem_name=telem_a), make_entry(telem_name=telem_b)])
        mocker.patch.object(query_telem, 'CosmosAPIClient', return_value=mocker.Mock())
        mocker.patch.object(
            query_telem, 'verify_wait_telemetry',
            return_value=[make_results([
                make_predict_result(telem_a, actual_value='INHIBIT', verification_status='PASS'),
                make_predict_result(telem_b, actual_value='INHIBIT', verification_status='PASS'),
            ])],
        )

        query_telem.main()

        output = last_output_dict(mock_io)
        assert telem_a in output['output_summary']
        assert telem_b in output['output_summary']

    def test_channel_variables_updated_with_latest_actual_values(self, mocker, mock_io):
        telem_a = 'TESTPKT__GENERIC/CHANNEL_THREE__FIELD_C'
        telem_b = 'TESTPKT__GENERIC/CHANNEL_FOUR__FIELD_D'
        set_input(
            mocker,
            [make_entry(telem_name=f'1234,{telem_a}'), make_entry(telem_name=telem_b)],
            states={'variables': {}, 'channel_variables': {telem_a: 'OLD'}},
        )
        mocker.patch.object(query_telem, 'CosmosAPIClient', return_value=mocker.Mock())
        mocker.patch.object(
            query_telem, 'verify_wait_telemetry',
            return_value=[make_results([
                make_predict_result(telem_a, actual_value='INHIBIT', verification_status='PASS'),
                make_predict_result(telem_b, actual_value='INHIBIT', verification_status='PASS'),
            ])],
        )

        query_telem.main()

        output = last_output_dict(mock_io)
        assert output['states']['channel_variables'] == {
            telem_a: 'INHIBIT',
            telem_b: 'INHIBIT',
        }

    def test_channel_variables_created_when_absent_from_input_states(self, mocker, mock_io):
        telem_name = 'TESTPKT__GENERIC/CHANNEL_ONE__FIELD_A'
        set_input(mocker, [make_entry(telem_name=telem_name)])
        mocker.patch.object(query_telem, 'CosmosAPIClient', return_value=mocker.Mock())
        mocker.patch.object(
            query_telem, 'verify_wait_telemetry',
            return_value=[make_results([
                make_predict_result(telem_name, actual_value=42, verification_status='PASS'),
            ])],
        )

        query_telem.main()

        assert last_output_dict(mock_io)['states']['channel_variables'] == {telem_name: 42}

    def test_channel_variables_preserved_when_no_value_returned(self, mocker, mock_io):
        telem_name = 'TESTPKT__GENERIC/CHANNEL_ONE__FIELD_A'
        set_input(mocker, [make_entry(telem_name=telem_name)],
                  states={'channel_variables': {telem_name: 'OLD'}})
        mocker.patch.object(query_telem, 'CosmosAPIClient', return_value=mocker.Mock())
        mocker.patch.object(
            query_telem, 'verify_wait_telemetry',
            return_value=[make_results([
                make_predict_result(telem_name, actual_value='', verification_status='FAIL'),
            ])],
        )

        query_telem.main()

        assert last_output_dict(mock_io)['states']['channel_variables'] == {telem_name: 'OLD'}

    def test_write_output_file_called_after_each_snapshot(self, mocker, mock_io):
        telem_name = 'TESTPKT__GENERIC/CHANNEL_ONE__FIELD_A'
        set_input(mocker, [make_entry(telem_name=telem_name)])
        mocker.patch.object(query_telem, 'CosmosAPIClient', return_value=mocker.Mock())
        mocker.patch.object(
            query_telem, 'verify_wait_telemetry',
            return_value=[make_results([
                make_predict_result(telem_name, actual_value='INHIBIT', verification_status='PASS'),
            ])],
        )

        query_telem.main()

        # Initial write + one write per yielded snapshot + final write
        assert mock_io.call_count >= 3
