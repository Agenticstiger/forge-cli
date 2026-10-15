# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Authentication utilities for API access"""

import os
from typing import Any, Dict, Mapping, Optional


def _credential(value: Any) -> Optional[str]:
    """``value`` when it is a non-empty string; anything else is no credential.

    ``false``, ``0``, ``[]`` or ``!!binary ""`` from a YAML config carry no
    credential, and neither does a number or a list: a header value is a
    string, and every falsy one used to read as "missing" and be refilled
    from the environment.
    """
    if isinstance(value, str) and value.strip():
        return value
    return None


def _env_credential(env_var: str, env_fallback: bool) -> Optional[str]:
    """``env_var``'s value when the caller may fall back to the environment and it is set."""
    return (os.getenv(env_var) or None) if env_fallback else None


def get_auth_headers(
    endpoint: str,
    auth_config: Optional[Mapping[str, Any]] = None,
    *,
    env_fallback: bool = True,
) -> Dict[str, str]:
    """Generate authentication headers for API requests

    Supports multiple auth methods:
    - API Key (X-API-Key header)
    - Bearer Token (Authorization header)
    - Basic Auth (Authorization header)

    Args:
        endpoint: API endpoint URL
        auth_config: Authentication configuration dict with keys:
            - type: 'api_key', 'bearer', 'basic'
            - api_key: API key value
            - token: Bearer token value
            - username: Basic auth username
            - password: Basic auth password
        env_fallback: Whether a credential the block does not carry (anything
            but a non-empty string) is read from FLUID_API_KEY /
            FLUID_BEARER_TOKEN. ``False`` for an endpoint a config file in the
            checkout named (``CatalogConfig.project_endpoint_file``): the
            runner's credential never goes there, whatever the block holds.

    Returns:
        Dictionary of HTTP headers
    """
    headers = {"Content-Type": "application/json", "Accept": "application/json"}

    if not isinstance(auth_config, Mapping) or not auth_config:
        # Try environment variables as fallback
        api_key = _env_credential("FLUID_API_KEY", env_fallback)
        if api_key:
            headers["X-API-Key"] = api_key

        bearer_token = _env_credential("FLUID_BEARER_TOKEN", env_fallback)
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"

        return headers

    auth_type = auth_config.get("type", "api_key")

    if auth_type == "api_key":
        api_key = _credential(auth_config.get("api_key")) or _env_credential(
            "FLUID_API_KEY", env_fallback
        )
        if api_key:
            headers["X-API-Key"] = api_key

    elif auth_type == "bearer":
        token = _credential(auth_config.get("token")) or _env_credential(
            "FLUID_BEARER_TOKEN", env_fallback
        )
        if token:
            headers["Authorization"] = f"Bearer {token}"

    elif auth_type == "basic":
        import base64

        username = auth_config.get("username", "")
        password = auth_config.get("password", "")
        credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
        headers["Authorization"] = f"Basic {credentials}"

    return headers


def validate_auth_config(auth_config: Dict[str, str]) -> bool:
    """Validate authentication configuration

    Args:
        auth_config: Authentication configuration dictionary

    Returns:
        True if valid, False otherwise
    """
    if not auth_config:
        # Check if environment variables are set
        return bool(os.getenv("FLUID_API_KEY") or os.getenv("FLUID_BEARER_TOKEN"))

    auth_type = auth_config.get("type", "api_key")

    if auth_type == "api_key":
        return "api_key" in auth_config or bool(os.getenv("FLUID_API_KEY"))

    elif auth_type == "bearer":
        return "token" in auth_config or bool(os.getenv("FLUID_BEARER_TOKEN"))

    elif auth_type == "basic":
        return "username" in auth_config and "password" in auth_config

    return False
