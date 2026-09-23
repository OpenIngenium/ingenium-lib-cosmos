"""
Ingenium custom script: run_script.py

This script runs scripts via the OpenC3 COSMOS Script Runner using the REST API.
It can run one or multiple scripts and monitor their execution.

"""

import sys
import copy
import time
from ing_lib.logs import get_logger,init_console_logger

# Log Level set via ING_LOG_LEVEL environment variable (defaults)
init_console_logger()
logger = get_logger(__name__)

from ing_lib.steps import get_input_output_paths, read_input_file, write_output_file
from ing_lib_cosmos.cosmos import CosmosAPIClient, IngeniumCosmosError

def main():
    """Main execution function"""
    # Load custom script input/output paths
    error_msg = 'USAGE: ./run_script.py input_file_path output_file_path'
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
        logger.info(f'Failed to initialize COSMOS client: {e}')
        output_dict['custom_script_status'] = 'ERROR'
        write_output_file(output_dict, output_file_abs_path)
        sys.exit(-1)

    # Process each script entry
    for i, entry in enumerate(entries):
        logger.info(f'\nProcessing script {i+1} of {len(entries)}')
        
        logger.debug(f"Processing script entry: {entry}")
    
        entry_inputs = entry.get('entry_inputs', {})
        script_name = entry_inputs.get('script_name', '')
        wait_for_completion = entry_inputs.get('wait_for_completion', True)
        timeout = int(entry_inputs.get('timeout', None))

        # Start the script. 
        try:
            start_results = client.start_script(script_name, timeout=timeout)
        except Exception as e:
            logger.error(f"Failed to start script: {e}")
            entry['verification_status'] = 'FAIL'
            entry['entry_outputs'] = {
                'success': False,
                'error': str(e)
            }
            write_output_file(output_dict, output_file_abs_path)
            continue

        logger.debug(f"Start results: {start_results}")

        running = start_results.get('running')
        script_id = start_results.get('script_id')

        entry['entry_outputs'] = {
            'running': running,
            'script_id': script_id,
            'timeout_remaining': timeout
        }

        logger.info(f"Script started: {script_id}")

        write_output_file(output_dict, output_file_abs_path)

        if wait_for_completion:
            script_details = {}
            result = {}
            try:
                for result in client.monitor_script(script_id, timeout=timeout):
                    logger.debug(f"Monitor status: {result}")

                    # Intermediate updates carry a raw 'script' dict; terminal
                    # updates (from _handle_script_*) do not, so script_details
                    # retains the last known values.
                    if result.get('script'):
                        script_details = result.get('script')

                    # Update entry outputs
                    entry['entry_outputs'] = {
                        'running': result.get('running', ''),
                        'script_id': result.get('script_id', script_id),
                        'state': result.get('state', ''),
                        'timeout_remaining': result.get('timeout_remaining', 0),
                        'start_time': script_details.get('start_time', ''),
                        'end_time': script_details.get('end_time', ''),
                        'cur_line_no': result.get('line_no', script_details.get('line_no', '')),
                        'start_line_no': script_details.get('start_line_no', ''),
                        'end_line_no': script_details.get('end_line_no', ''),
                        'last_update_time': script_details.get('updated_at', ''),
                        'error': result.get('error', '')
                    }

                    if script_details.get('errors'):
                        logger.error(f"Script execution encountered errors: {script_details.get('errors')}")

                    write_output_file(output_dict, output_file_abs_path)
            except IngeniumCosmosError as e:
                logger.error(f"Failed to monitor script: {e}")
                entry['verification_status'] = 'FAIL'
                entry['entry_outputs']['error'] = str(e)
                write_output_file(output_dict, output_file_abs_path)
                continue

            if result.get('state') in ('completed', 'done'):
                entry['verification_status'] = 'PASS'
                logger.info(f"  ✓ Script execution successful")
            else:
                entry['verification_status'] = 'FAIL'
                logger.info(f"  ✗ Script execution failed: {result.get('state')}")

        else:

            # If the step doesn't wait for completion, get the current status and continue.
            result = client.get_script(script_id)
            logger.debug(f"Script get result: {result}")

            script_details = result.get('script') or {}

            # Update entry outputs
            entry['entry_outputs'] = {
                'found': result.get('found', False),
                'script_id': script_id,
                'running': result.get('running', False),
                'state': result.get('state', ''),
                'timeout_remaining': result.get('timeout_remaining', timeout),
                'start_time': script_details.get('start_time', ''),
                'end_time': script_details.get('end_time', ''),
                'cur_line_no': result.get('line_no', ''),
                'start_line_no': script_details.get('start_line_no', ''),
                'end_line_no': script_details.get('end_line_no', ''),
                'last_update_time': script_details.get('updated_at', ''),
            }

            if result.get('found'):
                entry['verification_status'] = 'PASS'
                logger.info(f"  ✓ Script execution started")
            else:
                entry['verification_status'] = 'FAIL'
                entry['entry_outputs']['error'] = 'Script not found'
                logger.info(f"  ✗ Script not found: {script_id}")

            write_output_file(output_dict, output_file_abs_path)

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
