# `ing_lib_cosmos`

[![Tests](https://github.com/OpenIngenium/ingenium-lib-cosmos/actions/workflows/tests.yml/badge.svg)](https://github.com/OpenIngenium/ingenium-lib-cosmos/actions/workflows/tests.yml)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python Version](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

`ing_lib_cosmos` is the COSMOS integration layer for OpenIngenium. It provides a typed Python client for OpenC3 COSMOS authentication, JSON-RPC telemetry and command operations, and Script Runner operations.

The repository sits between two systems:

- **OpenC3 COSMOS**, which supplies command, telemetry, authentication, and Script Runner APIs.
- **Ingenium**, which supplies custom-script input/output conventions and reusable verification behavior through the pinned `ing_lib` dependency.

Ingenium custom-script steps built on this library live in the separate [`reference`](https://github.com/OpenIngenium/reference) repository, under `steps/cosmos/`. This repository stays focused on library code.

This README is intended to be a standalone, wiki-style reference for installing, configuring, using, extending, and troubleshooting the library.

---

## Contents

- [What is included](#what-is-included)
- [Requirements and compatibility](#requirements-and-compatibility)
- [Installation](#installation)
- [Configuration](#configuration)
- [Authentication](#authentication)
- [Python client API](#python-client-api)
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

The `ing_lib_cosmos.cosmos` module contains:

- `CosmosAuth`, including Enterprise and Core authentication modes.
- `CosmosAPIClient`, which lazily authenticates and exposes COSMOS operations.
- Typed exception classes for transport, authentication, JSON-RPC, and Script Runner failures.
- Telemetry adapters that translate COSMOS historical telemetry into the interface expected by `ing_lib.steps.verify_wait_telemetry`.
- Helpers for validating and constructing Ingenium telemetry queries.

---

## Requirements and compatibility

- Python **3.10 or newer**.
- An accessible OpenC3 COSMOS instance.
- A COSMOS scope, such as `DEFAULT`.
- Credentials appropriate for the selected authentication mode.
- Network access to the COSMOS base URL.
- The `ing_lib` package from the OpenIngenium repository. This repository currently pins it to tag `v0.1.1`.

The package metadata advertises Python 3.10 through 3.14. CI tests the same versions.

Runtime dependencies are:

- `requests>=2.31.0`
- `urllib3>=1.26.0`
- `ing_lib @ git+https://github.com/OpenIngenium/ingenium-lib.git@v0.1.1`

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

When changing behavior:

1. Trace the client flow before editing.
2. Add or update a focused test in `tests/`.
3. Preserve the public API surface consumed by downstream steps.
4. Run the complete test and lint commands before submitting a change.

The tests mock HTTP responses and do not require a live COSMOS server.

---

## Testing and continuous integration

Run the test suite with coverage:

```sh
pytest tests/ --cov=ing_lib_cosmos --cov-report=term-missing
```

Run the same focused lint used by CI:

```sh
python -m pip install 'ruff>=0.6.0'
ruff check ing_lib_cosmos tests
```

GitHub Actions runs tests on Python 3.10, 3.11, 3.12, 3.13, and 3.14. Pull requests and pushes to `main` install the pinned Ingenium library, install this package, run pytest with coverage, and run Ruff's `F` checks.

Tests cover:

- Authentication configuration and token refresh.
- Request, JSON parsing, and JSON-RPC error paths.
- Command and telemetry API payloads.
- Script lifecycle operations and timeout handling.
- Telemetry query construction and DN/EU behavior.

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

COSMOS updates its command counters asynchronously, so `get_cmd_cnt()` and `get_cmd_time()` can lag a `send_command()` call. Wait briefly before reading them, and verify the command result and COSMOS command history independently.

---

## Security and operational guidance

- Supply credentials through a secret manager, process environment, or deployment configuration; do not commit them.
- Do not log passwords, access tokens, authorization headers, or full secret-bearing payloads.
- Keep SSL verification enabled unless a controlled test explicitly requires otherwise.
- Treat `NO_CHECK` as an intentional operational choice. Prefer `ENABLED` for normal command execution when COSMOS command validation is available.
- Review command and Script Runner inputs before executing them against a flight or production system.
- Be cautious when retrying commands or script starts: the client avoids automatic retries for authenticated requests because repeating a non-idempotent operation can have side effects.
- `get_all_scripts()` and `halt_script()` operate within the configured COSMOS scope. Halting every script returned by `SCRIPT_FILTER_RUNNING` is a broad action; use it only when that is intended.

---

## Repository layout

```text
.
├── ing_lib_cosmos/
│   ├── __init__.py
│   └── cosmos.py
├── tests/
├── cosmos_env.sh
├── pytest.ini
├── ruff.toml
├── requirements.txt
├── requirements-dev.txt
├── setup.py
└── .github/workflows/tests.yml
```

`cosmos.py` is intentionally the central integration module: every COSMOS operation, exception type, and telemetry adapter lives there.

---

## Versioning and dependency pinning

The package version is currently `0.1.0` in both `setup.py` and `ing_lib_cosmos/__init__.py`.

The `ing_lib` dependency is pinned to the Git tag `v0.1.1` because verification behavior and input/output helpers are part of this package's compatibility surface. Update that pin deliberately and run the complete test suite when changing it.

The repository does not define console-script entry points; it is consumed as a library. Ingenium custom scripts that depend on it pin it by tag, as the [`reference`](https://github.com/OpenIngenium/reference) repository does in `steps/cosmos/requirements.txt`.

---

## License

See [LICENSE](LICENSE) for the repository's license terms.
