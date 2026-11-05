# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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
