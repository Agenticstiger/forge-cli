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
from typing import Dict, Optional


def get_auth_headers(endpoint: str, auth_config: Optional[Dict[str, str]] = None) -> Dict[str, str]:
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

    Returns:
        Dictionary of HTTP headers
    """
    headers = {"Content-Type": "application/json", "Accept": "application/json"}

    if not auth_config:
        # Try environment variables as fallback
        api_key = os.getenv("FLUID_API_KEY")
        if api_key:
            headers["X-API-Key"] = api_key

        bearer_token = os.getenv("FLUID_BEARER_TOKEN")
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"

        return headers

    auth_type = auth_config.get("type", "api_key")

    if auth_type == "api_key":
        api_key = auth_config.get("api_key") or os.getenv("FLUID_API_KEY")
        if api_key:
            headers["X-API-Key"] = api_key

    elif auth_type == "bearer":
        token = auth_config.get("token") or os.getenv("FLUID_BEARER_TOKEN")
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
