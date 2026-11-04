# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Filesystem helpers — reuses the ODCS provider's read/write helpers."""

from fluid_build.providers.odcs.io import read_input, write_output

__all__ = ["read_input", "write_output"]
