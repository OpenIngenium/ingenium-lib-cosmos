"""
COSMOS Authentication Module
Handles token generation and refresh for OpenC3 COSMOS
"""

import os
import urllib3
from typing import Tuple, Optional, Callable, List
import requests
import time
import traceback
import json
from typing import Dict, Any
from ing_lib.logs import get_logger
logger = get_logger(__name__)


class IngeniumCosmosError(Exception):
    """
    Base exception indicating the Ingenium Cosmos Library encountered an
    error and needs to abort. All other exceptions raised by this module
    subclass this.
    """
    pass


class CosmosConnectionError(IngeniumCosmosError):
    """Raised when a connection to the COSMOS server could not be established."""

    def __init__(self, message: str, url: Optional[str] = None):
        super().__init__(message)
        self.url = url


class CosmosTimeoutError(IngeniumCosmosError):
    """Raised when a request to the COSMOS server timed out."""

    def __init__(self, message: str, url: Optional[str] = None, timeout: Optional[int] = None):
        super().__init__(message)
        self.url = url
        self.timeout = timeout


class CosmosRequestError(IngeniumCosmosError):
    """Raised when the COSMOS server returned a non-successful HTTP response
    or the response body could not be parsed as expected."""

    def __init__(self, message: str, status_code: Optional[int] = None, response_text: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


class CosmosRPCError(CosmosRequestError):
    """Raised when a COSMOS JSON-RPC response contains an 'error' field."""

    def __init__(self, message: str, status_code: Optional[int] = None,
                 response_text: Optional[str] = None, rpc_error: Any = None):
        super().__init__(message, status_code=status_code, response_text=response_text)
        self.rpc_error = rpc_error


class CosmosAuthError(IngeniumCosmosError):
    """Raised when authentication with COSMOS fails."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class CosmosScriptError(IngeniumCosmosError):
    """Raised for Script Runner failures tied to a specific script_id."""

    def __init__(self, message: str, script_id: Optional[int] = None):
        super().__init__(message)
        self.script_id = script_id


# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Script Runner defaults (seconds)
DEFAULT_SCRIPT_TIMEOUT = 300
LOCK_TIMEOUT = 10
RUN_TIMEOUT = 30
STATUS_TIMEOUT = 10
POLL_INTERVAL = 1

# Supported COSMOS authentication modes
AUTH_MODE_ENTERPRISE = 'enterprise'
AUTH_MODE_CORE = 'core'
VALID_AUTH_MODES = (AUTH_MODE_ENTERPRISE, AUTH_MODE_CORE)

# Supported filters for get_all_scripts()
SCRIPT_FILTER_RUNNING = 'SCRIPT_FILTER_RUNNING'
SCRIPT_FILTER_COMPLETED = 'SCRIPT_FILTER_COMPLETED'
SCRIPT_FILTER_BOTH = 'SCRIPT_FILTER_BOTH'
VALID_SCRIPT_FILTERS = (SCRIPT_FILTER_RUNNING, SCRIPT_FILTER_COMPLETED, SCRIPT_FILTER_BOTH)

_TRUE_STRINGS = {'true', '1', 'yes', 'on'}
_FALSE_STRINGS = {'false', '0', 'no', 'off'}


def _resolve_bool_env_param(param_value: Optional[bool], env_var_name: str, default: bool = True) -> bool:
    """
    Resolve a boolean constructor parameter, falling back to an environment
    variable, falling back to a default.

    Args:
        param_value: The value explicitly passed by the caller (or None)
        env_var_name: The environment variable to fall back to if param_value is None
        default: The value to use if neither param_value nor the environment
            variable is set

    Returns:
        The resolved boolean value

    Raises:
        ValueError: If the environment variable is set but not a recognized
            boolean string
    """
    if isinstance(param_value, bool):
        return param_value

    if param_value is not None:
        # Caller passed a non-bool (e.g. a string); normalize it below
        raw = str(param_value)
    else:
        raw = os.getenv(env_var_name)
        if raw is None:
            return default

    normalized = raw.strip().lower()
    if normalized in _TRUE_STRINGS:
        return True
    if normalized in _FALSE_STRINGS:
        return False
    raise ValueError(
        f"{env_var_name} must be one of {sorted(_TRUE_STRINGS | _FALSE_STRINGS)}, got '{raw}'"
    )


def _resolve_env_param(param_value: Optional[str], env_var_name: str) -> str:
    """
    Resolve a constructor parameter, falling back to an environment variable.

    Args:
        param_value: The value explicitly passed by the caller (or None)
        env_var_name: The environment variable to fall back to if param_value is None

    Returns:
        The resolved value

    Raises:
        ValueError: If param_value is None and the environment variable is not set
    """
    if param_value is not None:
        return param_value

    value = os.getenv(env_var_name)
    if value is None:
        raise ValueError(
            f"{env_var_name} is not set and no value was provided"
        )
    return value


def _extract_jsonrpc_error(parsed: Any) -> Optional[Dict[str, Any]]:
    """
    Extract the JSON-RPC 'error' object from an already-parsed response body.

    Args:
        parsed: Parsed JSON body (typically a dict)

    Returns:
        The 'error' dict if present, or None
    """
    rpc_error = parsed.get('error') if isinstance(parsed, dict) else None

    if rpc_error:
        try:
            error_message = f"RPC Error: {rpc_error.get('message')}"
        except (KeyError, TypeError, AttributeError):
            error_message = f'RPC Error: {rpc_error}'
        
        try:
            data_error = parsed['error']['data'].get('message')
        except (KeyError, TypeError, AttributeError):
            data_error = None
    
        if data_error:
            error_message += f" - Data Error: {data_error}"
    else:
        error_message = None

    return error_message


def _extract_jsonrpc_error_message(text: str) -> Optional[str]:
    """
    Attempt to extract the JSON-RPC 'error.message' field from a raw
    response body (used when the HTTP status itself is non-200, e.g. a
    403 from an API gateway wrapping a JSON-RPC error payload).

    Args:
        text: Raw response body text

    Returns:
        The extracted error message, or None if it could not be parsed
    """
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    
    error = _extract_jsonrpc_error(parsed)

    return error


def _parse_jsonrpc(response: requests.Response, failure_message_prefix: str) -> Any:
    """
    Parse a COSMOS JSON-RPC response body.

    Args:
        response: The HTTP response containing the JSON-RPC envelope
        failure_message_prefix: Message prefix to use when 'error' is present

    Returns:
        The JSON-RPC 'result' value on success

    Raises:
        CosmosRequestError: If the body could not be parsed as JSON, or the
            body has neither a 'result' nor an 'error' field
        CosmosRPCError: If the body contains an 'error' field
    """
    try:
        json_response = response.json()
    except ValueError:
        logger.error(f"Failed to parse JSON response (status {response.status_code}): {response.text}")
        raise CosmosRequestError('Failed to parse JSON response', status_code=response.status_code,
                                  response_text=response.text)

    if 'result' in json_response:
        return json_response['result']
    elif 'error' in json_response:
        error = json_response['error']
        error_message = error.get('message', 'Unknown error') if isinstance(error, dict) else 'Unknown error'
        logger.error(f"{failure_message_prefix}: {error_message}")
        raise CosmosRPCError(f"{failure_message_prefix}: {error_message}", status_code=response.status_code,
                              response_text=response.text, rpc_error=error)
    else:
        logger.error(f"Unexpected response format (status {response.status_code}): {response.text}")
        raise CosmosRequestError('Unexpected response format', status_code=response.status_code,
                                  response_text=response.text)


def _request(verb: Callable[..., requests.Response], url: str, timeout: int,
              **kwargs: Any) -> requests.Response:
    """
    Perform an HTTP request, converting transport-level failures into
    typed exceptions.

    Args:
        verb: Bound request method (e.g. requests.post, session.get)
        url: Target URL
        timeout: Request timeout in seconds
        **kwargs: Additional keyword arguments passed to verb()

    Returns:
        The HTTP response

    Raises:
        CosmosTimeoutError: If the request timed out
        CosmosConnectionError: If a connection could not be established
        IngeniumCosmosError: On any other unexpected transport failure
    """
    try:
        response = verb(url, timeout=timeout, **kwargs)
        logger.debug(f"COSMOS API response [{response.status_code}] {url}: {response.text}")
        return response
    except requests.exceptions.Timeout:
        message = f'Connection to {url} timed out after {timeout} seconds'
        logger.error(message)
        raise CosmosTimeoutError(message, url=url, timeout=timeout)
    except requests.exceptions.ConnectionError:
        message = f'Could not connect to {url}'
        logger.error(message)
        raise CosmosConnectionError(message, url=url)
    except (CosmosTimeoutError, CosmosConnectionError):
        raise
    except Exception as e:
        logger.error(f"Unexpected error during request to {url}: {e}\n{traceback.format_exc()}")
        raise IngeniumCosmosError(f'Unexpected error during request to {url}: {e}') from e


class CosmosAuth:
    """Handles COSMOS authentication and token management"""

    def __init__(self, base_url: str = None, username: str = None, password: str = None,
                 verify_ssl: bool = None, auth_mode: str = None):
        """
        Initialize COSMOS authentication

        Args:
            base_url: COSMOS server URL. Falls back to the COSMOS_URL
                environment variable if not provided.
            username: Keycloak username (Enterprise mode only). Falls back to
                the COSMOS_USERNAME environment variable if not provided.
                Not required (and ignored) in Core mode.
            password: Keycloak password (Enterprise) or COSMOS Core password.
                Falls back to the COSMOS_PASSWORD environment variable if not
                provided.
            verify_ssl: Whether to verify SSL certificates. Falls back to the
                COSMOS_VERIFY_SSL environment variable, defaulting to True.
            auth_mode: 'enterprise' (Keycloak) or 'core' (simple password).
                Falls back to the COSMOS_AUTH_MODE environment variable,
                defaulting to 'enterprise'.

        Raises:
            ValueError: If base_url or password is not provided and the
                corresponding environment variable is not set, if username is
                missing in enterprise mode, or if auth_mode is invalid.
        """
        base_url = _resolve_env_param(base_url, 'COSMOS_URL')
        password = _resolve_env_param(password, 'COSMOS_PASSWORD')
        verify_ssl = _resolve_bool_env_param(verify_ssl, 'COSMOS_VERIFY_SSL', default=True)

        auth_mode = auth_mode or os.getenv('COSMOS_AUTH_MODE', AUTH_MODE_ENTERPRISE)
        auth_mode = auth_mode.strip().lower()
        if auth_mode not in VALID_AUTH_MODES:
            raise ValueError(f"COSMOS_AUTH_MODE must be one of {VALID_AUTH_MODES}, got '{auth_mode}'")

        if auth_mode == AUTH_MODE_ENTERPRISE:
            username = _resolve_env_param(username, 'COSMOS_USERNAME')
        else:
            # Core mode has no username concept; env var is not required
            username = username or os.getenv('COSMOS_USERNAME')

        self.base_url = base_url.rstrip('/')
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self.auth_mode = auth_mode
        self.access_token = None
        self.refresh_token = None
        self.token_expiry = 0

    def invalidate(self) -> None:
        """
        Clear any cached token, forcing the next get_valid_token() call to
        re-authenticate from scratch.
        """
        self.access_token = None
        self.refresh_token = None
        self.token_expiry = 0

    def get_token_enterprise(self) -> str:
        """
        Get token for COSMOS Enterprise (Keycloak)

        Returns:
            The new access token

        Raises:
            CosmosAuthError: If authentication failed
            CosmosTimeoutError: If the request timed out
            CosmosConnectionError: If a connection could not be established
        """
        url = f"{self.base_url}/auth/realms/openc3/protocol/openid-connect/token"
        headers = {'Content-Type': 'application/x-www-form-urlencoded'}
        data = {
            'username': self.username,
            'password': self.password,
            'client_id': 'api',
            'grant_type': 'password'
        }

        response = _request(requests.post, url, 10, data=data, headers=headers, verify=self.verify_ssl)

        if response.status_code == 200:
            token_data = response.json()
            access_token = token_data.get('access_token')
            refresh_token = token_data.get('refresh_token')
            expires_in = token_data.get('expires_in', 300)

            # Store for automatic refresh
            self.access_token = access_token
            self.refresh_token = refresh_token
            self.token_expiry = time.time() + expires_in - 30  # Refresh 30s before expiry

            return access_token
        else:
            rpc_message = _extract_jsonrpc_error_message(response.text)
            message = f"Authentication failed: {response.status_code}"
            if rpc_message:
                message += f': {rpc_message}'
            logger.error(f"{message} - {response.text}")
            raise CosmosAuthError(message, status_code=response.status_code)

    def refresh_access_token(self) -> str:
        """
        Use refresh token to get a new access token

        Returns:
            The refreshed access token

        Raises:
            CosmosAuthError: If no refresh token is available, or the
                refresh request failed
            CosmosTimeoutError: If the request timed out
            CosmosConnectionError: If a connection could not be established
        """
        if not self.refresh_token:
            raise CosmosAuthError('No refresh token available', status_code=400)

        url = f"{self.base_url}/auth/realms/openc3/protocol/openid-connect/token"
        headers = {'Content-Type': 'application/x-www-form-urlencoded'}
        data = {
            'client_id': 'api',
            'grant_type': 'refresh_token',
            'refresh_token': self.refresh_token
        }

        response = _request(requests.post, url, 10, data=data, headers=headers, verify=self.verify_ssl)

        if response.status_code == 200:
            token_data = response.json()
            access_token = token_data.get('access_token')
            refresh_token = token_data.get('refresh_token')
            expires_in = token_data.get('expires_in', 300)

            # Update stored tokens
            self.access_token = access_token
            self.refresh_token = refresh_token
            self.token_expiry = time.time() + expires_in - 30

            return access_token
        else:
            rpc_message = _extract_jsonrpc_error_message(response.text)
            message = f"Token refresh failed: {response.status_code}"
            if rpc_message:
                message += f': {rpc_message}'
            logger.error(f"{message} - {response.text}")
            raise CosmosAuthError(message, status_code=response.status_code)

    def get_valid_token(self) -> str:
        """
        Get a valid token, refreshing automatically if needed

        Returns:
            A currently-valid access token

        Raises:
            CosmosAuthError: If no credentials are available, or
                authentication failed
            CosmosTimeoutError: If a request timed out
            CosmosConnectionError: If a connection could not be established
        """
        # If we have a token and it's still valid, return it
        if self.access_token and time.time() < self.token_expiry:
            return self.access_token

        if self.auth_mode == AUTH_MODE_CORE:
            if not self.password:
                raise CosmosAuthError('No credentials available for authentication', status_code=400)
            logger.info("Getting new Core access token...")
            return self.get_token_core()

        # Enterprise mode: if we have a refresh token, try to refresh
        if self.refresh_token:
            logger.info("Token expired, refreshing...")
            try:
                token = self.refresh_access_token()
                logger.info("Token refreshed successfully")
                return token
            except IngeniumCosmosError as e:
                logger.warning(f"Refresh failed: {e}, getting new token...")

        # Otherwise, get a new token
        if not self.username or not self.password:
            raise CosmosAuthError('No credentials available for authentication', status_code=400)

        logger.info("Getting new access token...")
        token = self.get_token_enterprise()
        logger.info("New token obtained")
        return token

    def get_token_core(self, password: str = None) -> str:
        """
        Get token for COSMOS Core (simple password)

        Args:
            password: COSMOS Core password. Defaults to self.password if
                not provided.

        Returns:
            The new access token

        Raises:
            CosmosAuthError: If authentication failed
            CosmosTimeoutError: If the request timed out
            CosmosConnectionError: If a connection could not be established
        """
        password = password or self.password
        url = f"{self.base_url}/openc3-api/auth/verify"
        headers = {'Content-Type': 'application/json'}
        payload = {'password': password}

        response = _request(requests.post, url, 10, json=payload, headers=headers, verify=self.verify_ssl)

        if response.status_code == 200:
            token = response.text.strip().strip('"')
            self.access_token = token
            # Core tokens carry no expiry; treat as non-expiring and rely on
            # 401 responses to trigger re-verification.
            self.token_expiry = float('inf')
            return token
        else:
            rpc_message = _extract_jsonrpc_error_message(response.text)
            message = f"Authentication failed: {response.status_code}"
            if rpc_message:
                message += f': {rpc_message}'
            logger.error(f"{message} - {response.text}")
            raise CosmosAuthError(message, status_code=response.status_code)


class CosmosAPIClient:
    """Client for interacting with OpenC3 COSMOS REST API"""

    def __init__(self, base_url: str = None, scope: str = None, username: str = None,
                 password: str = None, verify_ssl: bool = None, auth_mode: str = None,
                 auth: 'CosmosAuth' = None):
        """
        Initialize COSMOS API client. Authentication is embedded: the client
        owns a CosmosAuth instance and logs in lazily on the first API call
        (no network I/O happens here in __init__).

        Args:
            base_url: Base URL of COSMOS server (e.g., "http://localhost:2900").
                Falls back to the COSMOS_URL environment variable if not provided.
            scope: COSMOS scope. Falls back to the COSMOS_SCOPE environment
                variable if not provided.
            username: Keycloak username (Enterprise mode only). Falls back to
                the COSMOS_USERNAME environment variable if not provided.
            password: Keycloak password (Enterprise) or COSMOS Core password.
                Falls back to the COSMOS_PASSWORD environment variable if not
                provided.
            verify_ssl: Whether to verify SSL certificates. Falls back to the
                COSMOS_VERIFY_SSL environment variable, defaulting to True.
            auth_mode: 'enterprise' or 'core'. Falls back to the
                COSMOS_AUTH_MODE environment variable, defaulting to 'enterprise'.
            auth: Optional pre-built CosmosAuth instance to reuse/share across
                clients. If provided, base_url/username/password/verify_ssl/
                auth_mode are ignored in favor of the supplied instance.

        Raises:
            ValueError: If base_url or scope is not provided and the
                corresponding environment variable is not set, or if the
                resolved auth credentials/mode are invalid.
        """
        base_url = _resolve_env_param(base_url, 'COSMOS_URL')
        scope = _resolve_env_param(scope, 'COSMOS_SCOPE')
        verify_ssl = _resolve_bool_env_param(verify_ssl, 'COSMOS_VERIFY_SSL', default=True)

        self.base_url = base_url.rstrip('/')
        self.scope = scope
        self.verify_ssl = verify_ssl

        self.auth = auth if auth is not None else CosmosAuth(
            base_url=self.base_url,
            username=username,
            password=password,
            verify_ssl=verify_ssl,
            auth_mode=auth_mode,
        )

        self.token = None
        self.headers = {
            'Content-Type': 'application/json'
        }

        # Session used by the Script Runner methods for connection pooling
        self.session = requests.Session()
        self.session.headers.update(self.headers)

    @property
    def access_token(self) -> Optional[str]:
        """Currently cached access token, if any (may be stale/expired)"""
        return self.token

    def login(self) -> str:
        """
        Force an authentication attempt now rather than waiting for the
        first API call.

        Returns:
            The current access token

        Raises:
            CosmosAuthError: If authentication failed
        """
        return self._ensure_token()

    def _ensure_token(self) -> str:
        """
        Ensure self.headers/self.session carry a currently-valid access
        token, refreshing/logging in via self.auth as needed.

        Returns:
            The currently-valid access token

        Raises:
            CosmosAuthError: If authentication failed
        """
        token = self.auth.get_valid_token()
        if token != self.token:
            self.token = token
            self.headers['Authorization'] = f'Bearer {token}'
            self.session.headers['Authorization'] = f'Bearer {token}'
        return token

    def _authed_request(self, verb: Callable[..., requests.Response], url: str, timeout: int,
                         **kwargs: Any) -> requests.Response:
        """
        Ensure a valid token is present, perform the request, and retry once
        (after invalidating the cached token) if the server responds 401/403.

        Args:
            verb: Bound request method (e.g. requests.post, self.session.get)
            url: Target URL
            timeout: Request timeout in seconds
            **kwargs: Additional keyword arguments passed to verb()

        Returns:
            The HTTP response

        Raises:
            CosmosAuthError: If authentication failed
            CosmosTimeoutError: If the request timed out
            CosmosConnectionError: If a connection could not be established
        """
        self._ensure_token()

        # Refresh headers on the kwargs in case caller passed explicit headers
        if 'headers' in kwargs and kwargs['headers'] is self.headers:
            kwargs['headers'] = self.headers

        response = _request(verb, url, timeout, **kwargs)

        if response.status_code in (401, 403):
            logger.info("Received %s, invalidating token and retrying once", response.status_code)
            self.auth.invalidate()
            self._ensure_token()
            if 'headers' in kwargs:
                kwargs['headers'] = self.headers
            response = _request(verb, url, timeout, **kwargs)

        return response

    def send_command(self, cmd_string: str, check: str) -> Any:
        """
        Send a command to COSMOS using JSON-RPC

        Args:
            cmd_string: Command string in format: "TARGET COMMAND with PARAM1 value, PARAM2 value"
            check: Check level: "NO_CHECK", "ENABLED" (default)

        Returns:
            The JSON-RPC result from COSMOS

        Raises:
            IngeniumCosmosError: If check is not a valid check level
            CosmosRequestError: If COSMOS returns a non-200 status or an
                unparseable body
            CosmosRPCError: If the JSON-RPC response contains an error
            CosmosAuthError: If authentication failed
            CosmosTimeoutError: If the request timed out
            CosmosConnectionError: If a connection could not be established
        """
        # COSMOS uses JSON-RPC protocol
        # Endpoint is /openc3-api/api
        url = f"{self.base_url}/openc3-api/api"

   
        if check == 'NO_CHECK':
            method = 'cmd_no_checks'
        elif check == 'ENABLED':
            method = 'cmd'
        else:
            msg = f"Invalid check level: {check}"
            logger.error(msg)
            raise IngeniumCosmosError(msg)

        # Build JSON-RPC payload
        # Method "cmd" takes command string as param, scope in keyword_params
        json_rpc_payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": [cmd_string],
            "id": 1,
            "keyword_params": {
                "scope": self.scope
            }
        }

        response = self._authed_request(requests.post, url, 30, json=json_rpc_payload, headers=self.headers,
                                         verify=self.verify_ssl)

        if response.status_code != 200:
            rpc_message = _extract_jsonrpc_error_message(response.text)
            message = f'Command failed with status {response.status_code}'
            if rpc_message:
                message += f': {rpc_message}'
            logger.error(f"{message}")
            logger.debug(f'{response.text}')
            raise CosmosRequestError(message, status_code=response.status_code, response_text=response.text)

        result = _parse_jsonrpc(response, 'Command failed')
        logger.info('Command sent successfully')
        return result

    def get_telemetry(self, tlm_points: List[str], timeout: int = 10) -> Dict[str, Any]:
        """
        Query COSMOS for the current values of one or more telemetry points
        using JSON-RPC API.
        Args:
            tlm_points: List of telemetry strings in "TARGET PACKET ITEM" format
                (e.g. ['INST HEALTH_STATUS TEMP1', 'INST HEALTH_STATUS TEMP2'])
            timeout: HTTP timeout in seconds
        Returns:
            Dict mapping each telemetry string in tlm_points to its retrieved value.

        Raises:
            CosmosRequestError: If COSMOS returns a non-200 status or an
                unparseable body
            CosmosRPCError: If the JSON-RPC response contains an error
            CosmosAuthError: If authentication failed
            CosmosTimeoutError: If the request timed out
            CosmosConnectionError: If a connection could not be established
        """
        # COSMOS uses JSON-RPC protocol
        # Endpoint is /openc3-api/api
        url = f"{self.base_url}/openc3-api/api"

        # Build JSON-RPC payload
        # Method "get_tlm_values" takes a list of telemetry strings as param, scope in keyword_params
        json_rpc_payload = {
            "jsonrpc": "2.0",
            "method": "tlm",
            "params": [tlm_points],
            "id": 1,
            "keyword_params": {
                "scope": self.scope
            }
        }

        logger.debug(f"Querying COSMOS API: {url}")
        logger.debug(f"Querying COSMOS API: {json_rpc_payload}")
        logger.debug(f"Telemetry query: {tlm_points}")

        response = self._authed_request(requests.post, url, timeout, json=json_rpc_payload, headers=self.headers,
                                         verify=self.verify_ssl)

        if response.status_code != 200:
            rpc_message = _extract_jsonrpc_error_message(response.text)
            error_msg = f"COSMOS API returned status {response.status_code}"
            if rpc_message:
                error_msg += f': {rpc_message}'
            logger.error(f"{error_msg}")
            logger.debug(f'{response.text}')
            raise CosmosRequestError(error_msg, status_code=response.status_code, response_text=response.text)

        values = _parse_jsonrpc(response, 'Telemetry query failed')
        result = dict(zip(tlm_points, values))
        logger.info(f"Retrieved telemetry for {len(tlm_points)} point(s)")
        return result

    def query_telemetry(self, items: List[List[str]], start_time: str = None, end_time: str = None,
                        timeout: int = 10) -> Any:
        """
        Query historical telemetry values for one or more items over a time range.

        Args:
            items: List of [TARGET, PACKET, ITEM] triples
            start_time: ISO-8601 start time (e.g. "2026-08-14T10:00:00Z")
            end_time: ISO-8601 end time (e.g. "2026-08-14T11:00:00Z")
            timeout: HTTP timeout in seconds

        Returns:
            The raw JSON-RPC result list from COSMOS

        Raises:
            CosmosRequestError: If COSMOS returns a non-200 status or an
                unparseable body
            CosmosRPCError: If the JSON-RPC response contains an error
            CosmosAuthError: If authentication failed
            CosmosTimeoutError: If the request timed out
            CosmosConnectionError: If a connection could not be established
        """
        url = f"{self.base_url}/openc3-api/api"


        json_rpc_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "get_tlm_values",
            "params": [items],
            "keyword_params": {
                "scope": self.scope
            }
        }

        # Add start_time and end_time to keyword_params if they are provided
        if start_time:
            json_rpc_payload["keyword_params"]["start_time"] = start_time
        if end_time:
            json_rpc_payload["keyword_params"]["end_time"] = end_time

        logger.debug(f"Querying COSMOS API: {url}")
        logger.info(f"Querying COSMOS API: {json_rpc_payload}")
        logger.debug(f"Telemetry values query: items={items}, start_time={start_time}, end_time={end_time}")

        response = self._authed_request(requests.post, url, timeout, json=json_rpc_payload, headers=self.headers,
                                         verify=self.verify_ssl)

        if response.status_code != 200:
            rpc_message = _extract_jsonrpc_error_message(response.text)
            error_msg = f"COSMOS API returned status {response.status_code}"
            if rpc_message:
                error_msg += f': {rpc_message}'
            logger.error(f"{error_msg}")
            raise CosmosRequestError(error_msg, status_code=response.status_code, response_text=response.text)

        result = _parse_jsonrpc(response, 'Telemetry values query failed')
        logger.info(f"Retrieved telemetry values for {len(items)} item(s)")
        return result

    def get_cmd_time(self, target: str, command: str = None, timeout: int = 10) -> Dict[str, Any]:
        """
        Get the time the most recent command was sent to a target, optionally
        filtered by command name, using the COSMOS get_cmd_time JSON-RPC API.

        Args:
            target: Target name (e.g., "HANDLE")
            command: Optional command name to filter by (e.g., "ECHO"). If not
                provided, COSMOS returns the time of the most recent command
                sent to the target regardless of command name.
            timeout: HTTP timeout in seconds

        Returns:
            Dict with 'target_name', 'command_name', and 'time' (combined
            epoch seconds, or None if no matching command has been sent).

        Raises:
            CosmosRequestError: If COSMOS returns a non-200 status or an
                unparseable body
            CosmosRPCError: If the JSON-RPC response contains an error
            CosmosAuthError: If authentication failed
            CosmosTimeoutError: If the request timed out
            CosmosConnectionError: If a connection could not be established
        """
        url = f"{self.base_url}/openc3-api/api"

        params = [target, command] if command else [target]

        json_rpc_payload = {
            "jsonrpc": "2.0",
            "method": "get_cmd_time",
            "params": params,
            "id": 1,
            "keyword_params": {
                "scope": self.scope
            }
        }

        logger.debug(f"Querying COSMOS API: {url}")
        logger.debug(f"get_cmd_time query: target={target}, command={command}")

        response = self._authed_request(requests.post, url, timeout, json=json_rpc_payload, headers=self.headers,
                                         verify=self.verify_ssl)

        if response.status_code != 200:
            rpc_message = _extract_jsonrpc_error_message(response.text)
            error_msg = f"COSMOS API returned status {response.status_code}"
            if rpc_message:
                error_msg += f': {rpc_message}'
            logger.error(f"{error_msg}")
            logger.debug(f'{response.text}')
            raise CosmosRequestError(error_msg, status_code=response.status_code, response_text=response.text)

        raw = _parse_jsonrpc(response, 'get_cmd_time failed')
        if isinstance(raw, (list, tuple)) and len(raw) == 4:
            target_name, command_name, time_sec, time_usec = raw
            combined_time = None
            if time_sec is not None and time_usec is not None:
                combined_time = time_sec + time_usec / 1_000_000
            result = {
                'target_name': target_name,
                'command_name': command_name,
                'time': combined_time,
            }
            logger.info(f"Retrieved cmd time for {target}"
                        f"{f' {command}' if command else ''}: {result}")
            return result
        else:
            logger.warning(f"Unexpected get_cmd_time result format: {raw}")
            return raw

    def get_cmd_cnt(self, target: str, command: str = None, timeout: int = 10) -> Dict[str, Any]:
        """
        Get the number of times a command has been sent to a target, optionally
        filtered by command name, using the COSMOS get_cmd_cnt JSON-RPC API.

        Args:
            target: Target name (e.g., "HANDLE")
            command: Optional command name to filter by (e.g., "ECHO"). If not
                provided, COSMOS returns the total command count for the
                target regardless of command name.
            timeout: HTTP timeout in seconds

        Returns:
            Dict with 'target_name', 'command_name', and 'count'.

        Raises:
            CosmosRequestError: If COSMOS returns a non-200 status or an
                unparseable body
            CosmosRPCError: If the JSON-RPC response contains an error
            CosmosAuthError: If authentication failed
            CosmosTimeoutError: If the request timed out
            CosmosConnectionError: If a connection could not be established
        """
        url = f"{self.base_url}/openc3-api/api"

        params = [target, command] if command else [target]

        json_rpc_payload = {
            "jsonrpc": "2.0",
            "method": "get_cmd_cnt",
            "params": params,
            "id": 1,
            "keyword_params": {
                "scope": self.scope
            }
        }

        logger.debug(f"Querying COSMOS API: {url}")
        logger.debug(f"get_cmd_cnt query: target={target}, command={command}")

        response = self._authed_request(requests.post, url, timeout, json=json_rpc_payload, headers=self.headers,
                                         verify=self.verify_ssl)

        if response.status_code != 200:
            rpc_message = _extract_jsonrpc_error_message(response.text)
            error_msg = f"COSMOS API returned status {response.status_code}"
            if rpc_message:
                error_msg += f': {rpc_message}'
            logger.error(f"{error_msg}")
            logger.debug(f'{response.text}')
            raise CosmosRequestError(error_msg, status_code=response.status_code, response_text=response.text)

        raw = _parse_jsonrpc(response, 'get_cmd_cnt failed')
        result = {
            'target_name': target,
            'command_name': command,
            'count': raw,
        }
        logger.info(f"Retrieved cmd count for {target}"
                    f"{f' {command}' if command else ''}: {result}")
        return result

    def start_script(self, script_name: str, environment: Optional[list] = None,
                     lock: bool = True, timeout: int = RUN_TIMEOUT) -> Dict[str, Any]:
        """
        Start a script via the COSMOS Script Runner REST API

        This call is non-blocking: it returns as soon as COSMOS reports the
        script id. Use monitor_script() to wait for completion.

        Args:
            script_name: Script name/path (e.g., "HANDLE/procedures/script.py")
            environment: Optional list of environment entries passed to the script
            lock: If True, lock the script before running it
            timeout: HTTP timeout for the run request (seconds)

        Returns:
            Dict with 'script_id' and 'running' (True)

        Raises:
            IngeniumCosmosError: If script_name fails basic path traversal validation
            CosmosRequestError: If COSMOS returns a non-200/201 status, or the
                script id could not be parsed from the response
            CosmosRPCError: If COSMOS returns a JSON error payload
            CosmosAuthError: If authentication failed
            CosmosTimeoutError: If the request timed out
            CosmosConnectionError: If a connection could not be established
        """
        # Validate script name (basic path traversal check)
        if '..' in script_name or script_name.startswith('/'):
            raise IngeniumCosmosError('Invalid script name: contains invalid characters')

        script_path = f"scripts/{script_name}"
        lock_url = f"{self.base_url}/script-api/{script_path}/lock?scope={self.scope}"
        run_url = f"{self.base_url}/script-api/{script_path}/run?scope={self.scope}"

        logger.info(f"Starting script: {script_name}")

        if lock:
            # Lock the script (prevents others from editing). Failure to lock
            # is non-fatal; log and continue.
            logger.debug("Locking script...")
            try:
                lock_response = self._authed_request(self.session.post, lock_url, LOCK_TIMEOUT, verify=self.verify_ssl)
                if lock_response.status_code not in [200, 201, 204]:
                    logger.warning(f"Lock returned {lock_response.status_code}")
            except IngeniumCosmosError as e:
                logger.warning(f"Lock request failed: {e}")

        response = self._authed_request(self.session.post, run_url, timeout, json={"environment": environment or []},
                                         verify=self.verify_ssl)

        if response.status_code not in [200, 201]:
            rpc_message = _extract_jsonrpc_error_message(response.text)
            message = f'Script execution failed with status {response.status_code}'
            if rpc_message:
                message += f': {rpc_message}'
            logger.error(f"{message}")
            logger.debug(f'{response.text}')
            raise CosmosRequestError(message, status_code=response.status_code, response_text=response.text)

        # Parse script ID
        try:
            script_id = int(response.text.strip())
        except ValueError:
            # Try parsing as JSON error response
            try:
                json_response = response.json()
                if 'error' in json_response:
                    error_msg = json_response['error'].get("message", "Unknown error")
                    logger.error(f"API Error: {error_msg}")
                    raise CosmosRPCError(f'Script execution failed: {error_msg}', status_code=response.status_code,
                                          response_text=response.text, rpc_error=json_response['error'])
            except json.JSONDecodeError:
                pass

            logger.error(f"Failed to parse script ID from response: {response.text}")
            raise CosmosRequestError('Failed to parse script ID from response', status_code=response.status_code,
                                      response_text=response.text)

        logger.info(f"Script started with ID: {script_id}")
        return {'script_id': script_id, 'running': True}

    def _build_script_status(self, script: Dict[str, Any], running: bool) -> Dict[str, Any]:
        """Normalize a raw running-script/completed-script JSON body into the
        standard get_script() status shape."""
        return {
            'found': True,
            'running': running,
            'state': script.get('state'),
            'line_no': script.get('line_no', 0),
            'script': script,
        }

    def get_script(self, script_id: int) -> Dict[str, Any]:
        """
        Get a script directly by ID, regardless of running/completion state

        Queries the running-script endpoint first; if the script is not
        currently running, queries the completed-script endpoint. Not being
        found in either endpoint is a valid, non-exceptional outcome (the
        script may not have been created yet, or may have been purged) and
        is reflected via 'found': False rather than an exception.

        Args:
            script_id: Script ID returned by start_script()

        Returns:
            Dict with 'found', 'running', 'state', 'line_no' and the raw
            'script' entry when available
        """
        running_url = f"{self.base_url}/script-api/running-script/{script_id}?scope={self.scope}"
        try:
            response = self._authed_request(self.session.get, running_url, STATUS_TIMEOUT, verify=self.verify_ssl)
            if response.status_code == 200:
                try:
                    script = response.json()
                    logger.debug(f"Running script response: {script}")
                    return self._build_script_status(script, running=True)
                except json.JSONDecodeError:
                    logger.warning(f"Could not parse running-script response for script {script_id}")
        except IngeniumCosmosError as e:
            logger.debug(f"running-script lookup failed for {script_id}: {e}")

        completed_url = f"{self.base_url}/script-api/completed-script/{script_id}?scope={self.scope}"
        try:
            response = self._authed_request(self.session.get, completed_url, STATUS_TIMEOUT, verify=self.verify_ssl)
            if response.status_code == 200:
                try:
                    script = response.json()
                    logger.debug(f"Completed script response: {script}")
                    return self._build_script_status(script, running=False)
                except json.JSONDecodeError:
                    logger.warning(f"Could not parse completed-script response for script {script_id}")
        except IngeniumCosmosError as e:
            logger.debug(f"completed-script lookup failed for {script_id}: {e}")

        return {'found': False, 'running': False, 'state': None, 'line_no': 0, 'script': None}

    def get_all_scripts(self, script_filter: str = SCRIPT_FILTER_BOTH) -> Dict[str, Any]:
        """
        Get all scripts known to COSMOS, filtered by running/completed state

        Queries the running-script and/or completed-script collection
        endpoints (no script ID), depending on script_filter.

        Args:
            script_filter: One of SCRIPT_FILTER_RUNNING, SCRIPT_FILTER_COMPLETED,
                or SCRIPT_FILTER_BOTH (default). Determines which endpoint(s)
                are queried.

        Returns:
            Dictionary containing 'running_scripts' (list) when script_filter
            is SCRIPT_FILTER_RUNNING or SCRIPT_FILTER_BOTH, and/or
            'completed_scripts' (list) when script_filter is
            SCRIPT_FILTER_COMPLETED or SCRIPT_FILTER_BOTH

        Raises:
            ValueError: If script_filter is not a valid filter
            CosmosRequestError: If COSMOS returns a non-200 status or an
                unparseable body
            CosmosAuthError: If authentication failed
            CosmosTimeoutError: If the request timed out
            CosmosConnectionError: If a connection could not be established
        """
        if script_filter not in VALID_SCRIPT_FILTERS:
            raise ValueError(
                f"Invalid script_filter '{script_filter}'; must be one of {VALID_SCRIPT_FILTERS}")

        result: Dict[str, Any] = {}

        if script_filter in (SCRIPT_FILTER_RUNNING, SCRIPT_FILTER_BOTH):
            running_url = f"{self.base_url}/script-api/running-script?scope={self.scope}"
            response = self._authed_request(self.session.get, running_url, STATUS_TIMEOUT,
                                              verify=self.verify_ssl)
            if response.status_code != 200:
                raise CosmosRequestError(
                    f"Failed to retrieve running scripts (status {response.status_code})",
                    status_code=response.status_code, response_text=response.text)
            try:
                result['running_scripts'] = response.json().get('items', [])
            except json.JSONDecodeError:
                raise CosmosRequestError("Could not parse running-script list response",
                                          status_code=response.status_code, response_text=response.text)

        if script_filter in (SCRIPT_FILTER_COMPLETED, SCRIPT_FILTER_BOTH):
            completed_url = f"{self.base_url}/script-api/completed-script?scope={self.scope}"
            response = self._authed_request(self.session.get, completed_url, STATUS_TIMEOUT,
                                              verify=self.verify_ssl)
            if response.status_code != 200:
                raise CosmosRequestError(
                    f"Failed to retrieve completed scripts (status {response.status_code})",
                    status_code=response.status_code, response_text=response.text)
            try:
                result['completed_scripts'] = response.json().get('items', [])
            except json.JSONDecodeError:
                raise CosmosRequestError("Could not parse completed-script list response",
                                          status_code=response.status_code, response_text=response.text)

        return result

    def halt_script(self, script_id: int) -> Dict[str, Any]:
        """
        Stop a running script

        Args:
            script_id: Script ID returned by start_script()

        Returns:
            Dict with 'script_id', 'running' (False), and 'already_stopped'
            (True if the script was not running when the halt was requested)

        Raises:
            CosmosScriptError: If COSMOS returned a failure status other than
                "already stopped"
            CosmosAuthError: If authentication failed
            CosmosTimeoutError: If the request timed out
            CosmosConnectionError: If a connection could not be established
        """
        action = 'stop'
        url = f"{self.base_url}/script-api/running-script/{script_id}/{action}?scope={self.scope}"

        logger.info(f"Halting script {script_id} (action: {action})")
        try:
            response = self._authed_request(self.session.post, url, STATUS_TIMEOUT, verify=self.verify_ssl)
        except IngeniumCosmosError as e:
            raise CosmosScriptError(f"Failed to halt script {script_id}: {e}", script_id=script_id) from e

        if response.status_code in [200, 201, 204]:
            return {'script_id': script_id, 'running': False, 'already_stopped': False}

        if response.status_code == 404:
            logger.info(f"Script {script_id} was already stopped")
            return {'script_id': script_id, 'running': False, 'already_stopped': True}

        rpc_message = _extract_jsonrpc_error_message(response.text)
        message = f'Failed to halt script with status {response.status_code}'
        if rpc_message:
            message += f': {rpc_message}'
        logger.error(f"{message}")
        logger.debug(f'{response.text}')
        raise CosmosScriptError(message, script_id=script_id)

    def monitor_script(self, script_id: int, timeout: int = DEFAULT_SCRIPT_TIMEOUT,
                       poll_interval: int = POLL_INTERVAL):
        """
        Poll a running script until it reaches a terminal state, yielding
        each raw get_script() status dict as soon as it is generated. The
        last item yielded is the terminal result dict (success/error info).

        Args:
            script_id: Script ID returned by start_script()
            timeout: Maximum time to wait (seconds)
            poll_interval: Delay between status polls (seconds)

        Yields:
            Intermediate: raw get_script() dict ('found', 'running', 'state',
                'line_no', 'script') for each poll.
            Final: dict with completion status ('state', 'script_id',
                'running', and 'error' when applicable). 'error'/'stopped'
                states are yielded as data, not raised, since they are
                legitimate script outcomes.

        Raises:
            CosmosScriptError: If the script does not reach a terminal state
                within timeout (it is halted first, on a best-effort basis)
        """
        start_time = time.time()
        script_executed = False  # Track if script progressed beyond spawning

        logger.info(f"Waiting for script {script_id} completion (timeout: {timeout}s)...")

        while (time.time() - start_time) < timeout:
            status = self.get_script(script_id)
            yield status

            if status['found']:
                script = status['script']
                state = status['state']
                line_no = status['line_no']
                logger.info(f"Status: {state}, Line: {line_no}")

                # Track if script progressed beyond spawning
                if state in ['running', 'waiting', 'completed', 'done'] or line_no > 0:
                    script_executed = True

                if state == 'error':
                    yield self._handle_script_error(script, script_id)
                    return
                elif state == 'stopped':
                    yield self._handle_script_stopped(script_id)
                    return
                elif state in ['completed', 'done']:
                    yield self._handle_script_success(script_id, state)
                    return
            else:
                yield self._handle_script_not_found(script_id, script_executed)
                return

            time.sleep(poll_interval)

        logger.warning(f"Timeout waiting for script {script_id} completion - Halting it.")

        # Best-effort halt; a failure to halt should not mask the timeout.
        try:
            halted = self.halt_script(script_id)
            logger.debug(f"Script {script_id} halted: {halted}")
        except IngeniumCosmosError as e:
            logger.error(f"Failed to halt script {script_id} after timeout: {e}")

        raise CosmosScriptError(f"Script {script_id} timed out after {timeout}s", script_id=script_id)

    def _handle_script_error(self, script: Dict[str, Any], script_id: int) -> Dict[str, Any]:
        """Handle script in error state"""
        errors = script.get('errors', [])
        error_msg = '\n'.join(errors) if errors else 'Script failed with error state'

        logger.error(f"Script {script_id} failed with error")
        if errors:
            logger.error(f"Error output:\n{errors[0]}")

        return {'script_id': script_id, 'state': 'error', 'running': False, 'error': error_msg}

    def _handle_script_stopped(self, script_id: int) -> Dict[str, Any]:
        """Handle script that was stopped"""
        logger.warning(f"Script {script_id} was stopped")
        return {'script_id': script_id, 'state': 'stopped', 'running': False}

    def _handle_script_success(self, script_id: int, state: str) -> Dict[str, Any]:
        """Handle successful script completion"""
        logger.info(f"Script {script_id} completed")
        return {'script_id': script_id, 'state': state, 'running': False}

    def _handle_script_not_found(self, script_id: int, script_executed: bool) -> Dict[str, Any]:
        """Handle script not in running list"""
        if not script_executed:
            # Script never progressed beyond spawning - likely failed to load
            logger.error(f"Script {script_id} failed (never progressed beyond spawning)")
            return {'script_id': script_id, 'state': 'not_found', 'running': False,
                    'error': 'Script never progressed beyond spawning state'}

        # Script executed and completed - success
        logger.info(f"Script {script_id} completed (no longer in running list)")
        return {'script_id': script_id, 'state': 'completed', 'running': False}

