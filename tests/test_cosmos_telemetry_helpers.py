"""
Tests for the telemetry verification helpers in ing_lib_cosmos.cosmos:
split_channel_name, build_query_dict, build_query_telemetry_item,
build_packet_timeformatted_item, cosmos_telemetry_query_func.
"""
import pytest

from ing_lib.steps import check_telemetry_query
from ing_lib_cosmos.cosmos import (
    split_channel_name, build_query_dict,
    build_query_telemetry_item, build_packet_timeformatted_item,
    cosmos_telemetry_query_func, validate_dn_eu,
)


# ---------------------------------------------------------------------------
# split_channel_name
# ---------------------------------------------------------------------------

class TestSplitChannelName:
    def test_basic_split(self):
        target, packet, tlm_point = split_channel_name(
            'TESTPKT__GENERIC/CHANNEL_ONE__FIELD_A'
        )
        assert target == 'TESTPKT'
        assert packet == 'GENERIC/CHANNEL_ONE'
        assert tlm_point == 'FIELD_A'

    def test_missing_delimiter_raises(self):
        with pytest.raises(ValueError):
            split_channel_name('TESTPKT_GENERIC_ITEM')

    def test_too_many_delimiters_raises(self):
        with pytest.raises(ValueError):
            split_channel_name('A__B__C__D')


# ---------------------------------------------------------------------------
# build_query_dict
# ---------------------------------------------------------------------------

class TestBuildQueryDict:
    def test_basic_predict_without_prior_value(self):
        entry_inputs = {
            'telem_name': 'TESTPKT__GENERIC/CHANNEL_ONE__FIELD_A',
            'verify_wait': 'VERIFY',
            'dn_eu': 'EU',
            'verification_condition': 'EQUAL',
            'verification_values': ['INHIBIT'],
        }
        predict = build_query_dict(entry_inputs)

        assert predict['telem_uuid'] == entry_inputs['telem_name']
        assert predict['verify_wait'] == 'VERIFY'
        assert predict['dn_eu'] == 'EU'
        assert predict['verification_condition'] == 'EQUAL'
        assert predict['verification_values'] == ['INHIBIT']
        assert 'prior_value' not in predict
        # Should be a valid predict per check_telemetry_query (expects a list)
        check_telemetry_query([predict])

    def test_prior_value_included_when_provided(self):
        entry_inputs = {
            'telem_name': 'TESTPKT__GENERIC/CHANNEL_ONE__FIELD_B',
            'verify_wait': 'VERIFY',
            'dn_eu': 'EU',
            'verification_condition': 'EQUAL',
            'verification_values': [1],
        }
        predict = build_query_dict(entry_inputs, prior_value=4)

        assert predict['prior_value'] == 4
        check_telemetry_query([predict])

    def test_bit_mask_and_bit_op_included_when_present(self):
        entry_inputs = {
            'telem_name': 'TESTPKT__GENERIC/CHANNEL_ONE__STATUS_WORD',
            'verify_wait': 'VERIFY',
            'dn_eu': 'DN',
            'verification_condition': 'EQUAL',
            'verification_values': [1],
            'bit_mask': '0x1',
            'bit_op': 'AND',
        }
        predict = build_query_dict(entry_inputs)

        assert predict['bit_mask'] == '0x1'
        assert predict['bit_op'] == 'AND'
        check_telemetry_query([predict])

    def test_inclusive_range_predict_is_valid(self):
        entry_inputs = {
            'telem_name': 'TESTPKT__GENERIC/CHANNEL_ONE__ANALOG_STATE.FIELD_C',
            'verify_wait': 'VERIFY',
            'dn_eu': 'EU',
            'verification_condition': 'INCLUSIVE_RANGE',
            'verification_values': [-217, -215],
        }
        predict = build_query_dict(entry_inputs)
        check_telemetry_query([predict])


# ---------------------------------------------------------------------------
# validate_dn_eu
# ---------------------------------------------------------------------------

class TestValidateDnEu:
    def test_valid_dn_records_seen(self):
        seen = {}
        validate_dn_eu('TESTPKT__GENERIC/CHANNEL_ONE__A', 'DN', seen)
        assert seen == {'TESTPKT__GENERIC/CHANNEL_ONE__A': 'DN'}

    def test_valid_eu_records_seen(self):
        seen = {}
        validate_dn_eu('TESTPKT__GENERIC/CHANNEL_ONE__A', 'EU', seen)
        assert seen == {'TESTPKT__GENERIC/CHANNEL_ONE__A': 'EU'}

    def test_invalid_dn_eu_raises(self):
        with pytest.raises(ValueError):
            validate_dn_eu('TESTPKT__GENERIC/CHANNEL_ONE__A', 'BOGUS', {})

    def test_same_telem_name_same_dn_eu_allowed(self):
        seen = {}
        telem_name = 'TESTPKT__GENERIC/CHANNEL_ONE__A'
        validate_dn_eu(telem_name, 'DN', seen)
        validate_dn_eu(telem_name, 'DN', seen)
        assert seen == {telem_name: 'DN'}

    def test_same_telem_name_conflicting_dn_eu_raises(self):
        seen = {}
        telem_name = 'TESTPKT__GENERIC/CHANNEL_ONE__A'
        validate_dn_eu(telem_name, 'DN', seen)
        with pytest.raises(ValueError):
            validate_dn_eu(telem_name, 'EU', seen)


# ---------------------------------------------------------------------------
# build_query_telemetry_item
# ---------------------------------------------------------------------------

class TestBuildQueryTelemetryItem:
    def test_dn_appends_raw_suffix(self):
        result = build_query_telemetry_item('TESTPKT', 'GENERIC/CHANNEL_ONE', 'FIELD_A', 'DN')
        assert result == 'TESTPKT__GENERIC/CHANNEL_ONE__FIELD_A__RAW'

    def test_eu_appends_converted_suffix(self):
        result = build_query_telemetry_item('TESTPKT', 'GENERIC/CHANNEL_ONE', 'FIELD_A', 'EU')
        assert result == 'TESTPKT__GENERIC/CHANNEL_ONE__FIELD_A__CONVERTED'

    def test_invalid_dn_eu_raises(self):
        with pytest.raises(ValueError):
            build_query_telemetry_item('TESTPKT', 'GENERIC/CHANNEL_ONE', 'FIELD_A', 'BOGUS')


# ---------------------------------------------------------------------------
# build_packet_timeformatted_item
# ---------------------------------------------------------------------------

class TestBuildPacketTimeformattedItem:
    def test_basic(self):
        result = build_packet_timeformatted_item('TESTPKT', 'GENERIC/CHANNEL_ONE')
        assert result == 'TESTPKT__GENERIC/CHANNEL_ONE__PACKET_TIMEFORMATTED__RAW'


# ---------------------------------------------------------------------------
# cosmos_telemetry_query_func
# ---------------------------------------------------------------------------

class FakeHistoricalClient:
    """
    Stands in for CosmosAPIClient.query_telemetry(). `rows` mirrors the raw
    JSON-RPC result shape cosmos_telemetry_query_func expects: a list of
    samples (one per poll/timestamp), each sample a list positionally aligned
    with the requested `items`, with the value at index 0 of each per-item entry.
    """

    def __init__(self, rows=None, error=None):
        self.rows = rows if rows is not None else []
        self.error = error
        self.calls = []

    def query_telemetry(self, items, start_time=None, end_time=None, timeout=10):
        self.calls.append((items, start_time, end_time, timeout))
        if self.error:
            raise self.error
        return self.rows


def sample(*values):
    """Wrap positional values as a single time-sample row for FakeHistoricalClient."""
    return [[v] for v in values]


class TestMakeHistoricalTelemetryQueryFunc:
    def test_single_channel_zips_value_and_timestamp_series_sorted(self):
        channel_name = 'TESTPKT__GENERIC/CHANNEL_ONE__FIELD_A'
        client = FakeHistoricalClient(rows=[
            sample('B', '2026-01-01T00:00:02Z'),
            sample('A', '2026-01-01T00:00:01Z'),
        ])
        func = cosmos_telemetry_query_func(client, {channel_name: 'EU'})

        result = func([channel_name], 10, 0, '2026-01-01T00:00:00Z', None)

        history = result[channel_name]
        assert [obj['eng_value'] for obj in history] == ['A', 'B']
        assert [obj['time'] for obj in history] == ['2026-01-01T00:00:01Z', '2026-01-01T00:00:02Z']
        assert all('raw_value' not in obj for obj in history)

    def test_dn_channel_populates_raw_value_field(self):
        channel_name = 'TESTPKT__GENERIC/CHANNEL_ONE__A'
        client = FakeHistoricalClient(rows=[
            sample(1, '2026-01-01T00:00:00Z'),
            sample(2, '2026-01-01T00:00:01Z'),
        ])
        func = cosmos_telemetry_query_func(client, {channel_name: 'DN'})

        result = func([channel_name], 10, 0, '2026-01-01T00:00:00Z', None)

        history = result[channel_name]
        assert [obj['raw_value'] for obj in history] == [1, 2]
        assert all('eng_value' not in obj for obj in history)

    def test_multiple_channels_sharing_a_packet_query_timestamp_once(self):
        channel_a = 'TESTPKT__GENERIC/CHANNEL_ONE__A'
        channel_b = 'TESTPKT__GENERIC/CHANNEL_ONE__B'
        client = FakeHistoricalClient(rows=[
            sample(1, '2026-01-01T00:00:00Z', 2),
        ])
        func = cosmos_telemetry_query_func(client, {channel_a: 'EU', channel_b: 'EU'})

        result = func([channel_a, channel_b], 10, 0, '2026-01-01T00:00:00Z', None)

        assert result[channel_a][0]['eng_value'] == 1
        assert result[channel_b][0]['eng_value'] == 2
        queried_items = client.calls[0][0]
        time_item = 'TESTPKT__GENERIC/CHANNEL_ONE__PACKET_TIMEFORMATTED__RAW'
        assert queried_items.count(time_item) == 1

    def test_multiple_packets_use_their_own_timestamp_series(self):
        channel_a = 'TESTPKT__GENERIC/CHANNEL_ONE__A'
        channel_b = 'TESTPKT__OTHERCHANNEL__B'
        client = FakeHistoricalClient(rows=[
            sample(1, '2026-01-01T00:00:00Z', 2, '2026-01-01T00:00:05Z'),
        ])
        func = cosmos_telemetry_query_func(client, {channel_a: 'EU', channel_b: 'EU'})

        result = func([channel_a, channel_b], 10, 0, '2026-01-01T00:00:00Z', None)

        assert result[channel_a][0] == {'time': '2026-01-01T00:00:00Z', 'eng_value': 1}
        assert result[channel_b][0] == {'time': '2026-01-01T00:00:05Z', 'eng_value': 2}

    def test_multiple_samples_all_included_in_order(self):
        channel_name = 'TESTPKT__GENERIC/CHANNEL_ONE__A'
        client = FakeHistoricalClient(rows=[
            sample(1, '2026-01-01T00:00:00Z'),
            sample(2, '2026-01-01T00:00:01Z'),
            sample(3, '2026-01-01T00:00:02Z'),
        ])
        func = cosmos_telemetry_query_func(client, {channel_name: 'EU'})

        result = func([channel_name], 10, 0, '2026-01-01T00:00:00Z', None)

        history = result[channel_name]
        assert [obj['eng_value'] for obj in history] == [1, 2, 3]
