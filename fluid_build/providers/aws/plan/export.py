# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

# fluid_build/providers/aws/plan/export.py
"""Export FLUID contracts to external formats (OPDS, DOT)."""

from collections.abc import Mapping
from typing import Any, Dict, List


def export_opds(src: Mapping[str, Any] | List[Mapping[str, Any]]) -> Dict[str, Any]:
    """Export to Open Data Product Standard format."""
    return {
        "status": "ok",
        "message": "OPDS export not yet implemented for AWS provider",
    }


def export_dot_graph(src: Mapping[str, Any] | List[Mapping[str, Any]]) -> Dict[str, Any]:
    """Export to GraphViz DOT format."""
    return {
        "status": "ok",
        "message": "DOT export not yet implemented for AWS provider",
    }
