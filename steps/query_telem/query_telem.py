"""
Ingenium custom script: query_telem.py

This script checks telemetry values from COSMOS and compares them against predicted values.
Using the provided target and packet, the script queries COSMOS for the current value of a specified telemetry point and reports PASS if
the value matches the prediction, or FAIL if it does not match.

Note that this will not function on
"""

import sys
import copy
from datetime import datetime, timezone
from ing_lib.logs import get_logger,init_console_logger

# Log Level set via ING_LOG_LEVEL environment variable (defaults)
init_console_logger()
logger = get_logger(__name__)

from ing_lib.steps import (
    get_input_output_paths, read_input_file, write_output_file,
    verify_wait_telemetry, InputError, get_telem_prior_value,
    translate_verification_conditions,
)
from ing_lib_cosmos.cosmos import (
    CosmosAPIClient, IngeniumCosmosError, cosmos_telemetry_query_func, build_query_dict,
    validate_dn_eu, VALUE_TYPE_BY_DN_EU,
)


def parse_start_time(start_time_raw):
    """
    Parse the 'start_time' value from the top-level custom script inputs.

    Parameters
    ----------
    start_time_raw
        None, or a DOY timestamp in ``YYYY-DDDTHH:MM:SS`` format
        (for example, ``2026-266T21:24:01``)

    Returns
    -------
    A timezone-aware datetime object, or None if start_time_raw is None/empty.

    Raises
    ------
    InputError if start_time_raw is a non-empty string that cannot be parsed.
    """
    if not start_time_raw:
        return None

    try:
        parsed = datetime.strptime(start_time_raw, '%Y-%jT%H:%M:%S')
        return parsed.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError, AttributeError) as e:
        msg = f"Unable to parse start_time '{start_time_raw}': {e}"
        logger.error(msg)
        raise InputError(msg) from e


def parse_telem_name(telem_name_raw):
    """Extract and validate the canonical telemetry name from input CSV data.

    The custom-script input stores telemetry metadata as ``telem_id,telem_name``.
    Only the telemetry name is used to build COSMOS queries; the ID remains in
    the original entry input for output compatibility.
    """
    if not isinstance(telem_name_raw, str):
        raise InputError(
            f"Invalid telem_name {telem_name_raw!r}: expected 'telem_id,telem_name'"
        )
    logger.debug(f"telem_name_raw: {telem_name_raw}")
    telem_name = telem_name_raw.split(',', 1)[1].strip()
    logger.debug(f"telem_name: {telem_name}")
    if not telem_name:
        raise InputError(
            f"Invalid telem_name {telem_name_raw!r}: telemetry name is empty"
        )

    return telem_name


def build_combined_query(input_dict, entries):
    """
    Build a single combined query dict (and matching channel_name -> dn_eu map)
    from every entry's entry_inputs, sourcing prior_value for CHANGE checks from
    the input's pre-populated channel_variables state.

    Parameters
    ----------
    input_dict: dict
        The full custom script input dictionary
    entries: list
        The list of entries (each with 'entry_inputs')

    Returns
    -------
    (query, entry_map): tuple of (dict, dict)
        query: combined {channel_name: predict} dict ready for verify_wait_telemetry
        entry_map: {channel_name: dn_eu} map ready for make_historical_telemetry_query_func
    """
    query = []
    entry_map = {}
    seen_dn_eu = {}

    for entry in entries:
        entry_inputs = entry['entry_inputs']
        logger.debug(f"entry_inputs: {entry_inputs}")
        telem_name = parse_telem_name(entry_inputs['telem_name'])
        logger.debug(f"telem_name: {telem_name}")
        normalized_entry_inputs = {**entry_inputs, 'telem_name': telem_name}
        logger.debug(f"normalized_entry_inputs: {normalized_entry_inputs}")
        dn_eu = entry_inputs['dn_eu']
        logger.debug(f"dn_eu: {dn_eu}")
        prior_value = None

        validate_dn_eu(telem_name, dn_eu, seen_dn_eu)

        if entry_inputs.get('verify_on', 'VALUE') == 'CHANGE':
            prior_value = get_telem_prior_value(input_dict, telem_name)

        query.append(build_query_dict(normalized_entry_inputs, prior_value=prior_value))
        entry_map[telem_name] = dn_eu

    return query, entry_map


def format_verification_condition(verification_conditions, actual_value):
    """Translate an Ingenium verification condition into a display expression
    with the actual telemetry value substituted in."""
    translated = translate_verification_conditions(verification_conditions)
    condition = translated['verification_condition']
    values = translated['verification_values']

    operators = {
        'GREATER_THAN': '>',
        'GREATER_THAN_OR_EQUAL': '>=',
        'LESS_THAN': '<',
        'LESS_THAN_OR_EQUAL': '<=',
        'EQUAL': '==',
        'NOT_EQUAL': '!=',
    }
    if condition in operators:
        return f'{actual_value} {operators[condition]} {values[0]}'
    if condition == 'CONTAINS':
        return f'{values[0]} contained in {actual_value}'
    if condition == 'INCLUSIVE_RANGE':
        return f'{values[0]} <= {actual_value} <= {values[1]}'
    if condition == 'EXCLUSIVE_RANGE':
        return f'{values[0]} < {actual_value} < {values[1]}'
    if condition == 'RECORD':
        return f'Record {actual_value}'
    if condition == 'NOT_PRESENT':
        return 'telemetry is not present'

    raise InputError(f'Unknown Verification Condition: {condition}')


def update_channel_variables(output_dict, entries):
    """
    Record each entry's latest actual value in the output's
    ``states['channel_variables']`` map, keyed by canonical telemetry name.

    Entries without a value (never populated, e.g. NOT_PRESENT or an aborted
    verification) are skipped so an existing prior value is not clobbered.

    Updates output_dict in place.
    """
    channel_variables = output_dict.setdefault('states', {}).setdefault('channel_variables', {})

    for entry in entries:
        actual_value = entry.get('entry_outputs', {}).get('actual_value', '')
        if actual_value == '':
            continue
        channel_variables[parse_telem_name(entry['entry_inputs']['telem_name'])] = actual_value


def apply_telem_result_to_entry(entry, telem_result):
    """
    Populate an entry's verification_status/entry_outputs from the channel_result
    returned by verify_wait_telemetry for that entry's channel_name.

    ``measured_value`` is sourced from the raw sample (telem_details), which is
    the value as reported by COSMOS before any bit mask or CHANGE (prior value)
    evaluation is applied to produce ``actual_value``.

    Updates the entry in place.
    """
    telem_details = telem_result.get('telem_details') or {}
    value_type = VALUE_TYPE_BY_DN_EU.get(entry['entry_inputs']['dn_eu'])

    entry['verification_status'] = telem_result['verification_status']
    entry['entry_outputs']['measured_value'] = telem_details.get(value_type, '')
    entry['entry_outputs']['actual_value'] = telem_result['actual_value']
    entry['entry_outputs']['telem_time'] = telem_details.get('time')
    entry['entry_outputs']['telem_eval'] = format_verification_condition(
        entry['entry_inputs']['verification_condition'],
        telem_result['actual_value']
    )


# Main logic
def main():

    # Load custom script input/output paths
    error_msg = 'USAGE: ./query_telem.py input_file_path output_file_path'
    input_file_abs_path, output_file_abs_path = get_input_output_paths(error_msg)

    logger.info(f'input_file_abs_path: {input_file_abs_path}')
    logger.info(f'output_file_abs_path: {output_file_abs_path}')

    # Read the input file
    logger.info('Reading custom script inputs')
    input_dict = read_input_file(input_file_abs_path)

    # Initialize output data structure
    entries = copy.deepcopy(input_dict.get('entries', []))

    output_dict = {
        'states': copy.deepcopy(input_dict.get('states', {})),
        'custom_script_status': 'PENDING',
        'entries': entries
    }

    # Initialize each entry with pending status and empty outputs
    for entry in entries:
        entry['verification_status'] = 'PENDING'
        entry['entry_outputs'] = {
            'measured_value': '',
            'actual_value': '',
            'telem_time': '',
            'telem_eval': ''
        }

    # Write initial output
    write_output_file(output_dict, output_file_abs_path)
    logger.info('Output file was initialized')

    # Create COSMOS API client. Authentication is embedded and happens
    # lazily on the first API call below; requires env_var COSMOS_USERNAME and
    # COSMOS_PASSWORD (or just COSMOS_PASSWORD in 'core' auth mode) to be set 
    try:
        client = CosmosAPIClient()
    except Exception as e:
        logger.debug('COSMOS client initialization exception details', exc_info=True)
        logger.error(f'Failed to initialize COSMOS client: {e}')
        output_dict['custom_script_status'] = 'ERROR'
        write_output_file(output_dict, output_file_abs_path)
        sys.exit(-1)

    # Source start_time/timeout/lookback from the top-level custom script inputs
    script_inputs = input_dict.get('inputs', {})
    timeout = script_inputs.get('timeout', 60)
    lookback = script_inputs.get('lookback', 0)

    try:
        start_time = parse_start_time(script_inputs.get('start_time'))
        query, entry_map = build_combined_query(input_dict, entries)
    except (InputError, ValueError) as e:
        logger.error(f'Failed to build combined telemetry query: {e}')
        for entry in entries:
            entry['verification_status'] = 'ERROR'
        output_dict['output_summary'] = f'Telemetry query construction failed: {e}'
        output_dict['custom_script_status'] = 'ERROR'
        write_output_file(output_dict, output_file_abs_path)
        sys.exit(-1)

    output_summary = ''
    logger.info(
        'Starting telemetry verification: telemetry_points=%s timeout=%s lookback=%s',
        len(entries), timeout, lookback,
    )

    if entries:
        telemetry_query_func = cosmos_telemetry_query_func(client, entry_map)

        try:
            # verify_wait_telemetry is an iterator: it yields a snapshot after
            # each poll (including a single terminal snapshot for an empty/
            # already-complete query). Drain it fully, applying/writing each
            # snapshot as it arrives so the output file reflects live progress
            # while a WAIT is still polling, not just the final result.
            for results in verify_wait_telemetry(
                query, telemetry_query_func, start_time=start_time, timeout=timeout, lookback=lookback
            ):
                output_summary = ''
                for i, entry in enumerate(entries):
                    telem_name = parse_telem_name(entry['entry_inputs']['telem_name'])
                    entry_result = results['predict_results'][i]

                    apply_telem_result_to_entry(entry, entry_result)

                    # Add to output summary
                    output_summary += f"Queried Channel {telem_name} with status of {entry.get('verification_status')}\n"

                output_dict['output_summary'] = output_summary

                # Update intermediate status after each snapshot
                write_output_file(output_dict, output_file_abs_path)
        except (InputError, IngeniumCosmosError) as e:
            logger.error(f'Telemetry verification failed: {e}')
            for entry in entries:
                entry['verification_status'] = 'ERROR'
            output_dict['output_summary'] = f'Telemetry verification failed: {e}'
            output_dict['custom_script_status'] = 'ERROR'
            write_output_file(output_dict, output_file_abs_path)
            sys.exit(-1)

    # Publish the latest telemetry values as state for downstream steps
    update_channel_variables(output_dict, entries)

    # Determine overall custom_script_status
    statuses = [entry['verification_status'] for entry in entries]
    if 'ERROR' in statuses:
        custom_script_status = 'ERROR'
    elif 'FAIL' in statuses:
        custom_script_status = 'FAIL'
    else:
        custom_script_status = 'PASS'

    output_dict['custom_script_status'] = custom_script_status

    # Report final custom_script_status
    write_output_file(output_dict, output_file_abs_path)
    final_log = {
        'PASS': logger.info,
        'FAIL': logger.warning,
        'ERROR': logger.error,
    }[custom_script_status]
    final_log('Custom script completed with status: %s', custom_script_status)


if __name__ == '__main__':
    main()
