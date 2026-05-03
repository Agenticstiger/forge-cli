# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Day-2 operations surface for source-aligned acquisition.

Modules:
- ``status``         — `fluid status <product-id>` summary
- ``logs``           — `fluid logs <product-id> --component build|infra|server|worker|dlq`
- ``run_diff``       — `fluid run-diff <run-a> <run-b>` schema + row count delta
- ``retention``      — `fluid retention sweep` periodic cleanup
- ``doctor``         — `fluid doctor --scope ingestion|infra|catalog|all` health checks
- ``auth``           — `fluid auth login/test/rotate <secretRef>` credential ops
"""

from __future__ import annotations

from .auth import AuthBackend, AuthResult, KeychainBackend, login, rotate, verify_secret
from .doctor import (
    DoctorCheck,
    DoctorCheckResult,
    DoctorReport,
    DoctorScope,
    Severity,
    run_doctor,
)
from .logs import LogComponent, fetch_logs
from .retention import RetentionSummary, sweep_with_summary
from .run_diff import RunDiff, run_diff
from .status import RunSummary, StatusReport, build_status_report

__all__ = [
    # Status
    "build_status_report",
    "RunSummary",
    "StatusReport",
    # Logs
    "fetch_logs",
    "LogComponent",
    # Run diff
    "run_diff",
    "RunDiff",
    # Retention
    "sweep_with_summary",
    "RetentionSummary",
    # Doctor
    "run_doctor",
    "DoctorScope",
    "DoctorCheck",
    "DoctorCheckResult",
    "DoctorReport",
    "Severity",
    # Auth
    "login",
    "verify_secret",
    "rotate",
    "AuthResult",
    "AuthBackend",
    "KeychainBackend",
]
