# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Soda Core integration — shells out to the user-installed ``soda`` binary."""

from .runner import SodaNotInstalled, SodaResult, resolve_soda_executable, run_soda_scan

__all__ = [
    "SodaNotInstalled",
    "SodaResult",
    "resolve_soda_executable",
    "run_soda_scan",
]
