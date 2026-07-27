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

"""Shared, defensive accessors for the governance validators."""

from __future__ import annotations

from typing import Any, Dict, List


def iter_exposes(contract: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return ``contract["exposes"]`` as a list of dict entries, defensively.

    Returns ``[]`` when the field is absent or not a list, and skips any
    non-dict entry.

    The governance passes run *after* the JSON-schema pass, which has
    already produced the correct message for a malformed ``exposes``
    (``'probe_table' is not of type 'array'``). Iterating a bare string
    made ``for expose in contract["exposes"]`` walk characters and
    ``"p".get(...)`` raise ``AttributeError``, which surfaced as
    ``cli_unhandled_exception: 'str' object has no attribute 'get'`` and
    threw away the schema errors before they could be rendered. Mirrors
    ``cli/contract_validation.py::_iter_objects``.
    """
    value = contract.get("exposes")
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, dict)]
