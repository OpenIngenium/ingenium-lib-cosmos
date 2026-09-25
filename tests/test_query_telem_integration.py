"""Integration coverage for the real query_telem verification pipeline."""
import pytest

import query_telem
from ing_lib_cosmos.cosmos import CosmosConnectionError


def make_entry(telem_name, verification_condition='EQUAL,INHIBIT,,'):
    return {
        'entry_inputs': {
            'telem_name': telem_name,
            'verify_wait': 'VERIFY',
            'verify_on': 'VALUE',
            'dn_eu': 'EU',
            'verification_condition': verification_condition,
            'bit_mask': None,
            'bit_op': 'NONE',
        },
    }


def make_input(entries):
    return {'config': {}, 'entries': entries, 'inputs': {'timeout': 1, 'lookback': 0}}


def sample(*values):
    return [[value] for value in values]


@pytest.fixture(autouse=True)
def mock_io(mocker):
    mocker.patch.object(query_telem, 'get_input_output_paths',
                        return_value=('/tmp/in.json', '/tmp/out.json'))
    write_mock = mocker.patch.object(query_telem, 'write_output_file')
    return write_mock


def test_real_query_telem_pipeline_passes(mocker, mock_io):
    channel = 'TARGET__PACKET__VALUE'
    mocker.patch.object(query_telem, 'read_input_file', return_value=make_input([make_entry(channel)]))
    client = mocker.Mock()
    client.query_telemetry.return_value = [sample('INHIBIT', '2026-01-01T00:00:00Z')]
    mocker.patch.object(query_telem, 'CosmosAPIClient', return_value=client)

    query_telem.main()

    output = mock_io.call_args_list[-1].args[0]
    assert output['custom_script_status'] == 'PASS'
    assert output['entries'][0]['verification_status'] == 'PASS'
    assert output['entries'][0]['entry_outputs']['actual_value'] == 'INHIBIT'
    assert output['entries'][0]['entry_outputs']['telem_time'] == '2026-01-01T00:00:00Z'


def test_real_pipeline_keeps_prefix_overlapping_packets_separate(mocker, mock_io):
    channel_a = 'TARGET__FSW/HK__A'
    channel_b = 'TARGET__FSW/HK_EXTENDED__B'
    entries = [make_entry(channel_a), make_entry(channel_b)]
    mocker.patch.object(query_telem, 'read_input_file', return_value=make_input(entries))
    client = mocker.Mock()
    client.query_telemetry.return_value = [
        sample(1, '2026-01-01T00:00:00Z', 2, '2026-01-01T00:00:05Z'),
    ]
    mocker.patch.object(query_telem, 'CosmosAPIClient', return_value=client)

    query_telem.main()

    output = mock_io.call_args_list[-1].args[0]
    assert output['custom_script_status'] == 'FAIL'
    assert output['entries'][0]['entry_outputs']['telem_time'] == '2026-01-01T00:00:00Z'
    assert output['entries'][1]['entry_outputs']['telem_time'] == '2026-01-01T00:00:05Z'
    queried_items = client.query_telemetry.call_args.args[0]
    assert queried_items.count('TARGET__FSW/HK__PACKET_TIMEFORMATTED__RAW') == 1
    assert queried_items.count('TARGET__FSW/HK_EXTENDED__PACKET_TIMEFORMATTED__RAW') == 1


def test_real_pipeline_marks_query_failure_error(mocker, mock_io):
    channel = 'TARGET__PACKET__VALUE'
    mocker.patch.object(query_telem, 'read_input_file', return_value=make_input([make_entry(channel)]))
    client = mocker.Mock()
    client.query_telemetry.side_effect = CosmosConnectionError('cosmos down')
    mocker.patch.object(query_telem, 'CosmosAPIClient', return_value=client)

    with pytest.raises(SystemExit) as excinfo:
        query_telem.main()

    assert excinfo.value.code == -1
    output = mock_io.call_args_list[-1].args[0]
    assert output['custom_script_status'] == 'ERROR'
    assert output['entries'][0]['verification_status'] == 'ERROR'
    assert set(output['entries'][0]['entry_outputs']) == {'actual_value', 'telem_time'}
    assert 'cosmos down' in output['output_summary']
