#!/usr/bin/env python3
"""
Ingenium custom script: halt_all_scripts.py

This script runs queries for all running scripts running via the OpenC3 COSMOS Script Runner using the REST API.
It then halts each one.

"""

import sys
from ing_lib.logs import get_logger,init_console_logger

# Log Level set via ING_LOG_LEVEL environment variable (defaults)
init_console_logger()
logger = get_logger(__name__)

from ing_lib.steps import get_input_output_paths, read_input_file, write_output_file
from ing_lib_cosmos.cosmos import CosmosAPIClient, IngeniumCosmosError

def main():
    """Main execution function"""
    # Load custom script input/output paths
    error_msg = 'USAGE: ./halt_all_scripts.py input_file_path output_file_path'
    input_file_abs_path, output_file_abs_path = get_input_output_paths(error_msg)

    logger.info(f'input_file_abs_path: {input_file_abs_path}')
    logger.info(f'output_file_abs_path: {output_file_abs_path}')

    # Read the input file
    logger.info('Reading custom script inputs')
    input_dict = read_input_file(input_file_abs_path)

    scripts_halted=[]

    outputs = {'scripts_running': 0,
             'scripts_halted': 0}

    output_dict = {
        'custom_script_status': 'PENDING',
        'inputs': input_dict,
        'outputs': outputs,
        'output_array': scripts_halted,
        'output_summary': {}
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

    # Query for all running scripts
    try:
        results = client.get_all_scripts(script_filter='SCRIPT_FILTER_RUNNING')
    except IngeniumCosmosError as e:
        logger.error(f'Failed to query running scripts: {e}')
        output_dict['custom_script_status'] = 'ERROR'
        write_output_file(output_dict, output_file_abs_path)
        sys.exit(-1)

    try:
        running_scripts = results['running_scripts']
    except KeyError:
        logger.error('Failed to parse running scripts from API response')
        output_dict['custom_script_status'] = 'ERROR'
        write_output_file(output_dict, output_file_abs_path)
        sys.exit(-1)
    
    logger.info(f'Found {len(running_scripts)} running scripts')
    outputs['scripts_running'] = len(running_scripts)
    write_output_file(output_dict, output_file_abs_path)
    logger.debug(f'Running scripts: {running_scripts}')

    # Halt each script:
    for script in running_scripts:
        try:
            script_id = script.get('name')
            script_state = script.get('state')
            script_filename = script.get('filename')
            script_line_no = script.get('line_no')
            script_start_time = script.get('start_time')
            script_last_updated = script.get('updated_at')
        except Exception as e:
            logger.error(f'Failed to parse script information: {e}')
            output_dict['custom_script_status'] = 'ERROR'
            write_output_file(output_dict, output_file_abs_path)
            sys.exit(-1)

        logger.info(f'Halting script: {script_filename} (ID: {script_id})')
        try:
            halted = client.halt_script(script_id)
            halt_success = not halted.get('running', True)
        except IngeniumCosmosError as e:
            logger.warning(f'Failed to halt script: {script_filename} (ID: {script_id}): {e}')
            halt_success = False

        if halt_success:
            outputs['scripts_halted'] = outputs['scripts_halted'] + 1
        else:
            logger.warning(f'Failed to halt script: {script_filename} (ID: {script_id})')

        script_information = {
            'script_halted': halt_success,
            'script_id': script_id, 
            'state': script_state,
            'filename': script_filename,
            'line_no': script_line_no,
            'start_time': script_start_time,
            'last_updated': script_last_updated
        }

        scripts_halted.append(script_information)

        # Update output after each command
        write_output_file(output_dict, output_file_abs_path)

    # Determine overall status
    custom_script_status = 'PASS'
    for script in scripts_halted:
        if not script.get('script_halted'):
            custom_script_status = 'FAIL'
            break

    output_dict['custom_script_status'] = custom_script_status

    output_dict['output_summary'] = f"Halted {outputs['scripts_halted']} out of {outputs['scripts_running']} scripts running"

    # Report final custom_script_status
    logger.info(f'\nFinal status: {custom_script_status}')
    write_output_file(output_dict, output_file_abs_path)

if __name__ == '__main__':
    main()
