# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Databricks provider package (plan only; see ``provider.py``)."""

from typing import Callable, Dict

from .provider import DatabricksProvider


def auto_register(register: Callable[[str, object], None]) -> None:
    """Register the Databricks provider with the discovery system."""
    register("databricks", DatabricksProvider)


PROVIDERS: Dict[str, type] = {"databricks": DatabricksProvider}

__all__ = ["DatabricksProvider", "PROVIDERS", "auto_register"]
