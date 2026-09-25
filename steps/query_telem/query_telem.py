#!/usr/bin/env python
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
    verify_wait_telemetry, InputError, get_telem_prior_value
)
from ing_lib_cosmos.cosmos import (
    CosmosAPIClient, IngeniumCosmosError, cosmos_telemetry_query_func, build_query_dict,
    validate_dn_eu,
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

    The custom-script input stores telemetry metadata as ``telem_name,telem_id``.
    Only the telemetry name is used to build COSMOS queries; the ID remains in
    the original entry input for output compatibility.
    """
    if not isinstance(telem_name_raw, str):
        raise InputError(
            f"Invalid telem_name {telem_name_raw!r}: expected 'telem_name,telem_id'"
        )
    logger.debug(f"telem_name_raw: {telem_name_raw}")
    telem_name = telem_name_raw.split(',', 1)[0].strip()
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


def apply_telem_result_to_entry(entry, telem_result):
    """
    Populate an entry's verification_status/entry_outputs from the channel_result
    returned by verify_wait_telemetry for that entry's channel_name.

    Updates the entry in place.
    """
    telem_details = telem_result.get('telem_details') or {}

    entry['verification_status'] = telem_result['verification_status']
    entry['entry_outputs']['actual_value'] = telem_result['actual_value']
    entry['entry_outputs']['telem_time'] = telem_details.get('time')


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
        'custom_script_status': 'PENDING',
        'entries': entries
    }

    # Initialize each entry with pending status and empty outputs
    for entry in entries:
        entry['verification_status'] = 'PENDING'
        entry['entry_outputs'] = {
            'actual_value': '',
            'telem_time': ''
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
        logger.info(f'Failed to initialize COSMOS client: {e}')
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
    logger.info(f"Custom script completed with status: {custom_script_status}")


if __name__ == '__main__':
    main()
