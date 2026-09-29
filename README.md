# `ing_lib_cosmos`

`ing_lib_cosmos` is the COSMOS integration layer for OpenIngenium. It provides a typed Python client for OpenC3 COSMOS authentication, JSON-RPC telemetry and command operations, and Script Runner operations. It also ships the custom-script entry points used by Ingenium workflows.

The repository sits between two systems:

- **OpenC3 COSMOS**, which supplies command, telemetry, authentication, and Script Runner APIs.
- **Ingenium**, which supplies custom-script input/output conventions and reusable verification behavior through the pinned `ing_lib` dependency.

This README is intended to be a standalone, wiki-style reference for installing, configuring, using, extending, and troubleshooting the repository.

---

## Contents

- [What is included](#what-is-included)
- [Requirements and compatibility](#requirements-and-compatibility)
- [Installation](#installation)
- [Configuration](#configuration)
- [Authentication](#authentication)
- [Python client API](#python-client-api)
- [Custom-script contract](#custom-script-contract)
- [Built-in custom-script steps](#built-in-custom-script-steps)
- [Telemetry verification model](#telemetry-verification-model)
- [Error handling](#error-handling)
- [Logging](#logging)
- [Development](#development)
- [Testing and continuous integration](#testing-and-continuous-integration)
- [Troubleshooting](#troubleshooting)
- [Security and operational guidance](#security-and-operational-guidance)
- [Repository layout](#repository-layout)
- [Versioning and dependency pinning](#versioning-and-dependency-pinning)

---

## What is included

### Core library

The `ing_lib_cosmos.cosmos` module contains:

- `CosmosAuth`, including Enterprise and Core authentication modes.
- `CosmosAPIClient`, which lazily authenticates and exposes COSMOS operations.
- Typed exception classes for transport, authentication, JSON-RPC, and Script Runner failures.
- Telemetry adapters that translate COSMOS historical telemetry into the interface expected by `ing_lib.steps.verify_wait_telemetry`.
- Helpers for validating and constructing Ingenium telemetry queries.

### Ingenium custom-script steps

Each step is an executable Python script that accepts an input JSON path and an output JSON path:

| Step | Purpose |
| --- | --- |
| `send_command` | Send one or more COSMOS commands and record command count/time. |
| `query_telem` | Query current/historical telemetry and evaluate Ingenium verification conditions. |
| `run_script` | Start a COSMOS Script Runner script and optionally wait for completion. |
| `halt_all_scripts` | Find all running COSMOS scripts and attempt to stop each one. |

Each step directory also contains:

- `custom_script.xml`: Ingenium website/custom-script declaration.
- `input.json`: example input shape.
- `output.json`: example output shape.
- The step implementation itself.

---

## Requirements and compatibility

- Python **3.10 or newer**.
- An accessible OpenC3 COSMOS instance.
- A COSMOS scope, such as `DEFAULT`.
- Credentials appropriate for the selected authentication mode.
- Network access to the COSMOS base URL.
- The `ing_lib` package from the OpenIngenium repository. This repository currently pins it to tag `v0.1.0`.

The package metadata advertises Python 3.10 through 3.14. CI tests the same versions.

Runtime dependencies are:

- `requests>=2.31.0`
- `urllib3>=1.26.0`
- `ing_lib @ git+https://github.com/OpenIngenium/ingenium-lib.git@v0.1.0`

---

## Installation

### Install from a checkout

```sh
python -m pip install .
```

For editable development installation:

```sh
python -m pip install -e .
```

To install test dependencies as well:

```sh
python -m pip install -r requirements-dev.txt
python -m pip install -e .
```

`requirements-dev.txt` includes the runtime requirements plus `pytest`, `pytest-mock`, and `pytest-cov`.

### Verify the installation

```sh
python -c "from ing_lib_cosmos.cosmos import CosmosAPIClient; print('ing_lib_cosmos import OK')"
```

Constructing `CosmosAPIClient` requires `COSMOS_URL` and `COSMOS_SCOPE`, and also resolves authentication configuration. Construction does not perform network I/O; the first authenticated operation performs login lazily.

---

## Configuration

The client accepts configuration as constructor arguments. When an argument is omitted, the corresponding environment variable is used. Explicit constructor arguments take precedence over environment variables.

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `COSMOS_URL` | Yes | — | COSMOS base URL, for example `https://cosmos.example.org`. Trailing `/` is removed. |
| `COSMOS_SCOPE` | Yes for `CosmosAPIClient` | — | COSMOS scope sent in API requests, for example `DEFAULT`. |
| `COSMOS_USERNAME` | Enterprise only | — | Keycloak username. Not required by Core authentication. |
| `COSMOS_PASSWORD` | Yes | — | Keycloak password in Enterprise mode, or COSMOS Core password in Core mode. |
| `COSMOS_VERIFY_SSL` | No | `true` | TLS certificate verification. Accepted values are `true`, `1`, `yes`, `on`, `false`, `0`, `no`, and `off`, case-insensitively. |
| `COSMOS_AUTH_MODE` | No | `enterprise` | `enterprise` or `core`. |
| `ING_LOG_LEVEL` | No | Defined by `ing_lib` logging setup | Logging level used by the Ingenium logger. |

A minimal Enterprise configuration is:

```sh
export COSMOS_URL='https://cosmos.example.org'
export COSMOS_SCOPE='DEFAULT'
export COSMOS_USERNAME='your-user'
export COSMOS_PASSWORD='use-a-secret-manager'
export COSMOS_VERIFY_SSL='true'
export COSMOS_AUTH_MODE='enterprise'
```

A Core configuration does not require a username:

```sh
export COSMOS_URL='https://cosmos-core.example.org'
export COSMOS_SCOPE='DEFAULT'
export COSMOS_PASSWORD='use-a-core-password'
export COSMOS_AUTH_MODE='core'
export COSMOS_VERIFY_SSL='true'
```

The repository includes `cosmos_env.sh` as a historical csh-style template using `setenv`. Adapt it to the syntax of the shell being used; do not commit real credentials into that file.

### Constructor-based configuration

Environment variables are convenient for custom scripts, but applications can pass values explicitly:

```python
from ing_lib_cosmos.cosmos import CosmosAPIClient

client = CosmosAPIClient(
    base_url='https://cosmos.example.org',
    scope='DEFAULT',
    username='your-user',
    password='your-password',
    verify_ssl=True,
    auth_mode='enterprise',
)
```

To share an authentication object between clients, construct `CosmosAuth` once and pass it through `auth=`. When `auth` is supplied, the client uses that object's base URL, credentials, SSL setting, and mode.

---

## Authentication

### Enterprise mode

Enterprise mode is the default. It uses the OpenID Connect password grant at:

```text
/auth/realms/openc3/protocol/openid-connect/token
```

The access and refresh tokens are cached in `CosmosAuth`. Enterprise access tokens are refreshed automatically before expiry when a refresh token is available. If refresh fails, the client attempts a full username/password login.

Enterprise mode requires:

- `COSMOS_URL`
- `COSMOS_USERNAME`
- `COSMOS_PASSWORD`
- `COSMOS_SCOPE` when using `CosmosAPIClient`

### Core mode

Core mode verifies the password at:

```text
/openc3-api/auth/verify
```

The returned token is treated as non-expiring locally. A later HTTP `401` or `403` invalidates the cached token so the next request re-verifies the password. Core mode requires `COSMOS_PASSWORD`, but does not require `COSMOS_USERNAME`.

### Lazy and explicit login

This initializes a client without making a request:

```python
client = CosmosAPIClient()
```

The first API call authenticates automatically. Use `login()` when an application wants to validate credentials or establish a token before doing work:

```python
client.login()
print(client.access_token)  # The currently cached token
```

Do not print or persist access tokens in application logs.

### SSL verification

SSL verification is enabled by default. Set `COSMOS_VERIFY_SSL=false` only for a controlled development/test environment with a known reason, such as a locally generated certificate. Production deployments should keep certificate verification enabled.

---

## Python client API

Import the client and exceptions from `ing_lib_cosmos.cosmos`:

```python
from ing_lib_cosmos.cosmos import (
    CosmosAPIClient,
    CosmosAuth,
    CosmosAuthError,
    CosmosConnectionError,
    CosmosRequestError,
    CosmosRPCError,
    CosmosScriptError,
    CosmosTimeoutError,
    IngeniumCosmosError,
)
```

### Commands

`send_command(cmd_string, check)` sends a COSMOS JSON-RPC command. The command string uses COSMOS syntax with a space between target and command:

```python
result = client.send_command(
    'TARGET COMMAND with ARGUMENT 42, LABEL "Example"',
    'ENABLED',
)
```

Supported check values are:

- `ENABLED`: use COSMOS command checking (`cmd`).
- `NO_CHECK`: bypass command checking (`cmd_no_checks`).

The method returns the JSON-RPC `result` value. It raises `IngeniumCosmosError` for an invalid check level and request/RPC/authentication exceptions for failures returned by COSMOS.

Command metrics are available through:

```python
count = client.get_cmd_cnt('TARGET', 'COMMAND')
when = client.get_cmd_time('TARGET', 'COMMAND')
```

`get_cmd_cnt()` returns a dictionary containing `target_name`, `command_name`, and `count`. `get_cmd_time()` returns those names plus `time`, represented as combined epoch seconds or `None` when there is no matching command.

### Current telemetry

`get_telemetry()` queries one or more current telemetry points:

```python
values = client.get_telemetry([
    'TARGET PACKET ITEM_A',
    'TARGET PACKET ITEM_B',
])
```

It returns a dictionary keyed by the exact strings passed in. The method uses the COSMOS JSON-RPC `tlm` operation.

### Historical telemetry

`query_telemetry()` queries telemetry samples over a time range:

```python
samples = client.query_telemetry(
    items=[['TARGET', 'PACKET', 'ITEM']],
    start_time='2026-08-14T10:00:00Z',
    end_time='2026-08-14T11:00:00Z',
)
```

`items` is a list of `[TARGET, PACKET, ITEM]` triples. `start_time` and `end_time` are optional ISO-8601 strings. The raw JSON-RPC result is returned unchanged.

The higher-level `cosmos_telemetry_query_func()` adapter is normally preferable for Ingenium verification because it adds packet timestamps, chooses DN/EU representations, and returns the structure expected by `verify_wait_telemetry()`.

### Script Runner

Start a script without waiting for completion:

```python
started = client.start_script('TARGET/procedures/example.py')
script_id = started['script_id']
```

`start_script()` returns `{'script_id': ..., 'running': True}`. Script names must be relative and must not contain `..` or start with `/`. The optional `environment` list is passed to COSMOS, and `lock=True` (the default) attempts to lock the script before running it. A lock failure is logged and is non-fatal; failure to run the script is not.

Read a script's current or completed status:

```python
status = client.get_script(script_id)
```

The result includes `found`, `running`, `state`, `line_no`, and the raw `script` object when available. A script missing from both COSMOS collections is reported with `found=False` rather than raising an exception.

Wait for completion with the polling generator:

```python
for status in client.monitor_script(script_id, timeout=300, poll_interval=1):
    print(status['state'], status.get('timeout_remaining'))
```

The generator yields intermediate status snapshots and then one terminal result. Terminal `completed`/`done`, `error`, and `stopped` states are returned as data. If the timeout expires, the client makes a best-effort halt attempt and raises `CosmosScriptError`.

Stop one script:

```python
result = client.halt_script(script_id)
```

A successful response returns `running=False`. HTTP 404 is treated as already stopped and returns `already_stopped=True`.

List scripts:

```python
running = client.get_all_scripts('SCRIPT_FILTER_RUNNING')
completed = client.get_all_scripts('SCRIPT_FILTER_COMPLETED')
both = client.get_all_scripts('SCRIPT_FILTER_BOTH')
```

The result contains `running_scripts` and/or `completed_scripts` depending on the filter. The default filter is `SCRIPT_FILTER_BOTH`.

### Timeouts and request behavior

The client uses operation-specific HTTP timeouts:

- Authentication and token refresh: 10 seconds.
- Command requests: 30 seconds.
- Script lock/status/stop requests: 10 seconds.
- Script start requests: 30 seconds.
- Current and historical telemetry default: 10 seconds.
- Script monitoring: 300 seconds by default, controlled by the `timeout` argument.

A `401` or `403` invalidates the cached token for the next request but is not automatically retried. This avoids accidentally duplicating non-idempotent operations such as commands.

---

## Custom-script contract

All four entry points follow the Ingenium custom-script convention:

```sh
python steps/<step>/<step>.py input.json output.json
```

The scripts:

1. Read the input JSON path from the command line.
2. Create and initialize the output JSON file with a pending status.
3. Create a `CosmosAPIClient` using environment configuration.
4. Update output as work progresses, where supported.
5. Write a final `custom_script_status`.

The common top-level statuses are:

- `PENDING`: output was initialized but work is not complete.
- `PASS`: all entries or requested operations succeeded.
- `FAIL`: at least one operation completed but did not meet its success criteria.
- `ERROR`: setup, input, authentication, transport, or other fatal processing error prevented normal verification.

Per-entry `verification_status` values generally use `PENDING`, `PASS`, `FAIL`, or `ERROR`.

Input and output files are ordinary JSON. The XML files describe the fields and UI layout consumed by Ingenium; they do not replace the JSON files used by the executable scripts.

---

## Built-in custom-script steps

### `send_command`

Run it with:

```sh
python steps/send_command/send_command.py input.json output.json
```

Input shape:

```json
{
  "entries": [
    {
      "entry_inputs": {
        "command_string": "TARGET__COMMAND with ARG 42, LABEL \"Example\"",
        "cmd_check": "ENABLED"
      }
    }
  ]
}
```

The Ingenium-facing `TARGET__COMMAND` form is converted to COSMOS's `TARGET COMMAND` form. Arguments following `with` are preserved.

For each entry, the step:

1. Parses and validates the command string.
2. Sends the command.
3. Waits briefly for COSMOS command counters to update.
4. Reads command count and most recent command time.
5. Writes `cmd_status`, `cmd_cnt`, and an ISO-8601 `timestamp`.

A failed command gets `cmd_status: "fail"`, `cmd_cnt: 0`, and `timestamp: "N/A"`. The overall status is `FAIL` if any entry fails.

### `query_telem`

Run it with:

```sh
python steps/query_telem/query_telem.py input.json output.json
```

Input shape:

```json
{
  "states": {
    "variables": {
      "channel_variables": {
        "TARGET__PACKET__ITEM": 4
      }
    }
  },
  "inputs": {
    "start_time": "2026-08-17T22:00:00Z",
    "timeout": 600,
    "lookback": 10
  },
  "entries": [
    {
      "entry_inputs": {
        "telem_name": "TARGET__PACKET__ITEM",
        "verify_wait": "WAIT",
        "verify_on": "VALUE",
        "dn_eu": "EU",
        "verification_condition": "EQUAL,4,,",
        "bit_mask": null,
        "bit_op": null
      }
    }
  ]
}
```

Important fields:

- `telem_name`: canonical `TARGET__PACKET__TELEMPOINT`; an optional comma-separated telemetry ID may be present in platform input, but only the name is used to build the COSMOS query.
- `verify_wait`: controls whether the verification checks available data or waits for a matching value, according to the `ing_lib` verification implementation. The XML exposes `WAIT` and `VERIFY`.
- `verify_on`: `VALUE` evaluates the queried value; `CHANGE` compares against the prior Ingenium channel value.
- `dn_eu`: `DN` requests the raw value; `EU` requests the converted engineering value.
- `verification_condition`: Ingenium verification condition string, translated by `ing_lib`.
- `bit_mask` and `bit_op`: optional bitwise verification settings.
- `start_time`: optional reference time. The step parser currently expects day-of-year format `YYYY-DDDTHH:MM:SS`, for example `2026-266T21:24:01`; an empty value means no explicit reference time. The checked-in JSON fixture uses an ISO-style value, so deployments should use the format accepted by the installed step implementation or update the fixture and platform mapping together.
- `timeout`: verification/query window and request timeout input.
- `lookback`: historical lookback offset used by the Ingenium verification flow.

The step writes `actual_value` and `telem_time` for every entry while polling. It writes intermediate output snapshots so a long-running `WAIT` operation can be observed. Input errors or telemetry verification failures mark entries as `ERROR` and terminate with overall `ERROR`.

Once polling completes, the step publishes each entry's latest value to `states.channel_variables`, keyed by canonical telemetry name. Entries that never produced a value leave any existing state entry untouched.

```json
{
  "states": {
    "channel_variables": {
      "TARGET__PACKET__ITEM": 4
    }
  }
}
```

Note that the prior value used for `verify_on: CHANGE` is read from `states.variables.channel_variables` by `ing_lib.steps.get_telem_prior_value`, which is a different location than where this output is written.

### `run_script`

Run it with:

```sh
python steps/run_script/run_script.py input.json output.json
```

Input shape:

```json
{
  "entries": [
    {
      "entry_inputs": {
        "script_name": "TARGET/procedures/example.py",
        "wait_for_completion": true,
        "timeout": 300
      }
    }
  ]
}
```

Behavior:

- `script_name` is passed to `start_script()` and must be a relative COSMOS script path.
- `wait_for_completion: true` polls until a terminal state. A completed/done state is `PASS`; an error, stopped, not-found, or timeout outcome is `FAIL`.
- `wait_for_completion: false` returns after starting and checking the script. A script found by COSMOS is `PASS`, even if it is still running.
- `timeout` defaults to `DEFAULT_SCRIPT_TIMEOUT` (300 seconds) when omitted or falsey.

Output fields include the script ID, running state, COSMOS state, line numbers, timestamps, timeout remaining, and any error text returned by Script Runner.

### `halt_all_scripts`

Run it with:

```sh
python steps/halt_all_scripts/halt_all_scripts.py input.json output.json
```

The input is currently unused; an empty JSON object is sufficient:

```json
{}
```

The step lists running scripts using `SCRIPT_FILTER_RUNNING`, attempts to stop each one, and reports:

- `scripts_running`: number found at the start of the operation.
- `scripts_halted`: number successfully stopped.
- `output_array`: per-script details, including ID, state, filename, line number, start time, and last update.
- `output_summary`: a human-readable count summary.

The step returns `PASS` only when every discovered script is successfully halted. If no scripts are running, the counts are zero and the operation is successful.

---

## Telemetry verification model

The telemetry helpers bridge two data models:

1. COSMOS historical telemetry returns series of raw or converted values.
2. `ing_lib.steps.verify_wait_telemetry()` expects channel-keyed records containing values, timestamps, and status metadata.

### Channel naming

Telemetry channels use exactly two `__` delimiters:

```text
TARGET__PACKET__TELEMETRY_POINT
```

`split_channel_name()` returns `(target, packet, tlm_point)` and raises `ValueError` if the name does not contain exactly three components.

### DN and EU

Only `DN` and `EU` are valid representations. `build_query_telemetry_item()` maps them to COSMOS suffixes:

| Ingenium value | COSMOS item suffix |
| --- | --- |
| `DN` | `__RAW` |
| `EU` | `__CONVERTED` |

`validate_dn_eu()` also prevents the same channel from being requested as both DN and EU within one combined request.

### Packet timestamps

COSMOS telemetry samples are paired with the packet's `PACKET_TIMEFORMATTED` series. The adapter sorts samples oldest-to-newest so the most recent measurement is last, which is the ordering expected by Ingenium verification.

### Query construction helpers

The public helper functions are useful when integrating a new step:

```python
from ing_lib_cosmos.cosmos import (
    build_packet_timeformatted_item,
    build_query_dict,
    build_query_telemetry_item,
    split_channel_name,
    validate_dn_eu,
)
```

`build_query_dict()` translates an Ingenium verification condition and creates the prediction structure consumed by `verify_wait_telemetry()`. It can include a `prior_value` for `CHANGE` verification.

---

## Error handling

All library-specific exceptions derive from `IngeniumCosmosError`:

| Exception | Meaning | Useful attributes |
| --- | --- | --- |
| `CosmosConnectionError` | The server could not be reached. | `url` |
| `CosmosTimeoutError` | A request exceeded its timeout. | `url`, `timeout` |
| `CosmosRequestError` | COSMOS returned an unsuccessful response or malformed body. | `status_code`, `response_text` |
| `CosmosRPCError` | A JSON-RPC response contained an `error` field. | Request fields plus `rpc_error` |
| `CosmosAuthError` | Authentication or token refresh failed. | `status_code` |
| `CosmosScriptError` | Script Runner operation failed or timed out. | `script_id` |

A typical application boundary is:

```python
try:
    result = client.send_command('TARGET COMMAND', 'ENABLED')
except CosmosAuthError:
    # Report credentials or authorization configuration to the operator.
    raise
except CosmosTimeoutError:
    # Retry only when the operation is safe to repeat.
    raise
except IngeniumCosmosError:
    # Record the operation as failed without exposing credentials.
    raise
```

The client preserves status codes and response text on request failures where available. Treat response text as potentially sensitive operational data when logging or reporting it.

---

## Logging

Custom scripts call `init_console_logger()` and obtain loggers through `ing_lib.logs.get_logger()`. Set `ING_LOG_LEVEL` according to the logging conventions of the installed `ing_lib` version.

Useful levels during development include:

- `INFO`: lifecycle and operation results.
- `DEBUG`: request URLs, payload details, polling, and response details.
- `WARNING`: recoverable conditions such as a failed lock attempt.
- `ERROR`: failed operations and terminal errors.

Use `DEBUG` carefully in shared environments: request payloads and response bodies may contain mission or operational data. Never add credentials, access tokens, or passwords to logs.

---

## Development

Clone the repository and create an isolated environment before making changes:

```sh
git clone https://github.com/OpenIngenium/ingenium-lib-cosmos.git
cd ingenium-lib-cosmos
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
python -m pip install -e .
```

The codebase uses straightforward Python modules and executable step scripts. When changing behavior:

1. Trace the client or step flow before editing.
2. Add or update a focused test in `tests/`.
3. Preserve the input/output contract and intermediate output behavior.
4. Run the complete test and lint commands before submitting a change.

The tests mock HTTP responses and do not require a live COSMOS server. The integration-named telemetry test verifies adapter behavior using mocked client calls; it is not a substitute for a live-system smoke test.

---

## Testing and continuous integration

Run the test suite with coverage:

```sh
pytest tests/ --cov=ing_lib_cosmos --cov-report=term-missing
```

Run the same focused lint used by CI:

```sh
python -m pip install 'ruff>=0.6.0'
ruff check ing_lib_cosmos steps tests --select F
```

GitHub Actions runs tests on Python 3.10, 3.11, 3.12, 3.13, and 3.14. Pull requests and pushes to `main` install the pinned Ingenium library, install this package, run pytest with coverage, and run Ruff's `F` checks.

Tests cover:

- Authentication configuration and token refresh.
- Request, JSON parsing, and JSON-RPC error paths.
- Command and telemetry API payloads.
- Script lifecycle operations and timeout handling.
- Telemetry query construction and DN/EU behavior.
- Custom-script input/output and status handling.

---

## Troubleshooting

### `ValueError: COSMOS_URL is not set`

Set `COSMOS_URL`, or pass `base_url=` to `CosmosAuth`/`CosmosAPIClient`. `COSMOS_SCOPE` is similarly required by `CosmosAPIClient`.

### Enterprise authentication reports missing credentials

Enterprise mode requires both `COSMOS_USERNAME` and `COSMOS_PASSWORD`. Check `COSMOS_AUTH_MODE`; if the server is COSMOS Core, set it to `core` so a username is not required.

### Authentication succeeds but API calls return `401` or `403`

Check the user/password, scope, server mode, and token permissions. The client invalidates its cached token after these responses, but it deliberately does not retry the same request. Call the operation again only after determining that repeating it is safe.

### TLS or certificate failures

Keep `COSMOS_VERIFY_SSL=true` in production and install the correct CA chain. For isolated local testing only, `COSMOS_VERIFY_SSL=false` can disable verification, but it should not be used as a general production fix.

### `Invalid script name`

`start_script()` rejects absolute paths and paths containing `..`. Use a COSMOS-relative path such as `TARGET/procedures/example.py`.

### A script is reported as not found

The Script Runner may remove a completed script from the running collection before it appears in the completed collection, or a script may fail before progressing past spawning. Use the returned `state`, `found`, and `error` fields and inspect COSMOS Script Runner logs for the script ID.

### Telemetry query validation fails

Check all of the following:

- The channel has the exact `TARGET__PACKET__ITEM` format.
- `dn_eu` is exactly `DN` or `EU`.
- The same channel is not requested with conflicting DN/EU values.
- The verification condition matches the syntax supported by the installed `ing_lib` version.
- `start_time` and `timeout` describe a window in which COSMOS has samples.

### Command count or timestamp appears unchanged

The command step intentionally waits briefly after dispatch before reading `get_cmd_cnt()` and `get_cmd_time()`. If the server still processes commands asynchronously, verify the command result and COSMOS command history independently.

---

## Security and operational guidance

- Supply credentials through a secret manager, process environment, or deployment configuration; do not commit them.
- Do not log passwords, access tokens, authorization headers, or full secret-bearing payloads.
- Keep SSL verification enabled unless a controlled test explicitly requires otherwise.
- Treat `NO_CHECK` as an intentional operational choice. Prefer `ENABLED` for normal command execution when COSMOS command validation is available.
- Review command and Script Runner inputs before executing them against a flight or production system.
- Be cautious when retrying commands or script starts: the client avoids automatic retries for authenticated requests because repeating a non-idempotent operation can have side effects.
- `halt_all_scripts` affects every running script in the configured COSMOS scope. Use it only when that broad action is intended.

---

## Repository layout

```text
.
├── ing_lib_cosmos/
│   ├── __init__.py
│   └── cosmos.py
├── steps/
│   ├── send_command/
│   ├── query_telem/
│   ├── run_script/
│   └── halt_all_scripts/
├── tests/
├── cosmos_env.sh
├── requirements.txt
├── requirements-dev.txt
├── setup.py
└── .github/workflows/tests.yml
```

`cosmos.py` is intentionally the central integration module. The step scripts are thin orchestration layers that translate Ingenium JSON into client calls and back into the expected output structure.

---

## Versioning and dependency pinning

The package version is currently `0.1.0` in both `setup.py` and `ing_lib_cosmos/__init__.py`.

The `ing_lib` dependency is pinned to the Git tag `v0.1.0` because verification behavior and input/output helpers are part of this package's compatibility surface. Update that pin deliberately and run the complete test suite when changing it.

The repository does not define console-script entry points. Invoke the step files directly with Python, or register them through the Ingenium custom-script XML configuration.

---

## License

See [LICENSE](LICENSE) for the repository's license terms.
