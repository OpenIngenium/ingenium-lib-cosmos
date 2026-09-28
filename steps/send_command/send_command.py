"""
Ingenium custom script: send_command.py

This script sends commands to OpenC3 COSMOS via the JSON-RPC API.
It can send one or multiple commands and verify their execution.

"""

import sys
import copy
import time
from datetime import datetime, timezone
from ing_lib.logs import get_logger,init_console_logger

# Log Level set via ING_LOG_LEVEL environment variable (defaults)
init_console_logger()
logger = get_logger(__name__)

from ing_lib.steps import get_input_output_paths, read_input_file, write_output_file
from ing_lib_cosmos.cosmos import CosmosAPIClient, IngeniumCosmosError

def parse_command_string(raw_command):
    """Parse an Ingenium command into its target, name, and arguments."""
    if not isinstance(raw_command, str):
        raise ValueError('command_string must be a string')

    target, separator, command_part = raw_command.partition('__')
    if not separator or not target.strip() or not command_part.strip():
        raise ValueError("command_string must use the 'TARGET__COMMAND' format")

    command, args_separator, args = command_part.partition('with')
    command = command.strip()
    if not command:
        raise ValueError('command_string must include a command name')
    return target.strip(), command, args.strip() if args_separator else ''


def process_command_entry(client, entry):
    """
    client: CosmosAPIClient instance
    entry: Dictionary containing command information
    
    Updates the entry in place with command results

    This function:
    1. Translates command string format to API format
        - e.g. "SCIENCE__ACQUIRE with EXPOSURE 10" -> "SCIENCE ACQUIRE with EXPOSURE 10"
    2. Sends the command via COSMOS API
    3. Checks the command count and time of dispatch (with a small delay)
    """

    raw_command = entry['entry_inputs'].get('command_string', '')
    logger.debug(f'Command String (pre-process): {raw_command}')
    cmd_check = entry['entry_inputs'].get('cmd_check', '')

    try:
        cmd_tgt, cmd_cmd, cmd_args = parse_command_string(raw_command)
        command_string = f'{cmd_tgt} {cmd_cmd}'
        if cmd_args:
            command_string += f' with {cmd_args}'
        logger.debug(f'Command string: {command_string}')
        logger.debug(f'Command check: {cmd_check}')
        cmd_result = client.send_command(command_string, cmd_check)
        logger.debug(f'Command result: {cmd_result}')

        # Added a slight delay between the command dispatch and the cmd_cnt/cmd_time checks
        # This was done due to a possible race condition where the cmd_cnt/cmd_time checks
        # were happening before the command was fully processed (and COSMOS was returning 0)
        time.sleep(.25)

        cmd_count = client.get_cmd_cnt(cmd_tgt, cmd_cmd)
        cmd_time = client.get_cmd_time(cmd_tgt, cmd_cmd)
    except (IngeniumCosmosError, ValueError) as e:
        logger.error(f'Command failed to send: {e}')
        entry['entry_outputs']['cmd_status'] = 'fail'
        entry['verification_status'] = 'FAIL'
        entry['entry_outputs']['cmd_cnt'] = 0
        entry['entry_outputs']['timestamp'] = 'N/A'
        return

    entry['entry_outputs']['cmd_status'] = 'success'
    entry['verification_status'] = 'PASS'

    logger.debug(f'cmd_count: {cmd_count}')
    entry['entry_outputs']['cmd_cnt'] = cmd_count.get('count')

    logger.debug(f'cmd_time: {cmd_time}')
    cmd_time_sec = cmd_time.get('time')
    if cmd_time_sec is not None:
        utc_time = datetime.fromtimestamp(cmd_time_sec, tz=timezone.utc)
        entry['entry_outputs']['timestamp'] = utc_time.isoformat().replace('+00:00', 'Z')
    else:
        entry['entry_outputs']['timestamp'] = 'N/A'




# Main logic
def main():

    # Load custom script input/output paths
    error_msg = 'USAGE: ./cosmos_send_command.py input_file_path output_file_path'
    input_file_abs_path, output_file_abs_path = get_input_output_paths(error_msg)

    logger.info(f'input_file_abs_path: {input_file_abs_path}')
    logger.info(f'output_file_abs_path: {output_file_abs_path}')

    # Read the input file
    logger.info('Reading custom script inputs')
    input_dict = read_input_file(input_file_abs_path)

    # Initialize output data
    entries = copy.deepcopy(input_dict.get('entries', []))


    output_dict = {
        'custom_script_status': 'PENDING',
        'entries': entries
    }

    # Initialize entry statuses
    for entry in entries:
        entry['verification_status'] = 'PENDING'
        entry['entry_outputs'] = {}

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
    
    logger.info(f'Connected to COSMOS at: {client.base_url}')

    output_summary = ''

    # Process each command entry
    for i, entry in enumerate(entries):
        logger.info(f'Processing command {i+1} of {len(entries)}')
        
        process_command_entry(client, entry)
        logger.info(
            'Command completed: entry=%s/%s status=%s',
            i + 1, len(entries), entry.get('verification_status'),
        )

        logger.debug(f'Entry {i+1} processed: {entry}')

        # Build summary
        output_summary += f"Sent Command {entry['entry_inputs'].get('command_string')} with status of {entry.get('verification_status')}\n"
        output_dict['output_summary'] = output_summary

        # Update output after each command
        write_output_file(output_dict, output_file_abs_path)

    # Determine overall status
    custom_script_status = 'PASS'
    for entry in entries:
        if entry['verification_status'] == 'ERROR':
            custom_script_status = 'ERROR'
            break
        elif entry['verification_status'] == 'FAIL':
            custom_script_status = 'FAIL'
            break

    output_dict['custom_script_status'] = custom_script_status

    # Report final custom_script_status
    logger.info(f'\nFinal status: {custom_script_status}')
    write_output_file(output_dict, output_file_abs_path)

if __name__ == '__main__':
    main()