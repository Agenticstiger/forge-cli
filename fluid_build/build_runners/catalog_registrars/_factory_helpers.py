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

"""Small helpers shared by ``register_catalog_backend`` factories.

Keeps each registrar module's ``_build_<name>`` factory three lines
long instead of restating the same "pick a token out of any of the
accepted config shapes" logic per backend.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def pick_token(config: Dict[str, Any]) -> Optional[str]:
    """Pull an API token out of *config* in any of the accepted shapes:

    - top-level ``api_token`` / ``token``
    - nested ``auth.api_token`` / ``auth.token`` / ``auth.api_key`` /
      ``auth.bearer_token``

    Returns ``None`` when nothing is set so the registrar omits the
    ``Authorization`` header (matches the registrar's own behaviour for
    unauthenticated targets in tests).
    """
    for key in ("api_token", "token"):
        v = config.get(key)
        if v:
            return str(v)
    auth = config.get("auth") or {}
    if isinstance(auth, dict):
        for key in ("api_token", "token", "api_key", "bearer_token"):
            v = auth.get(key)
            if v:
                return str(v)
    return None


def pick_endpoint(config: Dict[str, Any], *fallback_keys: str, default: str = "") -> str:
    """Resolve the backend endpoint from *config*.

    Tries ``endpoint`` first, then ``url``, then each name in
    ``fallback_keys`` (e.g. ``account_url`` for Snowflake). Returns
    *default* when nothing is set so the registrar's own default —
    typically a ``*.test`` URL used only by HTTP-mocked unit tests —
    takes over.
    """
    for key in ("endpoint", "url", *fallback_keys):
        v = config.get(key)
        if v:
            return str(v)
    return default


def pick_int(config: Dict[str, Any], key: str, default: int) -> int:
    """Read an integer out of *config* tolerantly. Strings round-trip
    via ``int()``; anything unparseable falls back to *default*."""
    v = config.get(key, default)
    try:
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


__all__ = ["pick_token", "pick_endpoint", "pick_int"]
