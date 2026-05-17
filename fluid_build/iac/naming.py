# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Shared naming helpers for the IaC emitter plugins."""

from __future__ import annotations

from typing import Any


def safe_ident(value: Any) -> str:
    """Coerce an arbitrary string into a valid OpenTofu identifier.

    OpenTofu resource names allow letters, digits and underscores and may
    not start with a digit. Non-conforming characters become ``_``.
    """
    cleaned = "".join(c if (c.isalnum() or c == "_") else "_" for c in str(value))
    cleaned = cleaned.strip("_") or "x"
    if cleaned[0].isdigit():
        cleaned = f"r_{cleaned}"
    return cleaned
