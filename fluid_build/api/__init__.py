# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Public extension contract for forge-cli source-aligned acquisition.

This package is the **stable public API** that third-party runners and
providers target. SemVer applies to ``__api_version__``; a 2-minor-version
deprecation window is required for any breaking change.

Anything outside ``fluid_build.api`` is internal and may change without
notice. Anything inside is governed.
"""

from __future__ import annotations

from .catalog import CatalogRegistrar, RegistrationResult
from .cost import BudgetCap, ChargebackTag, CostTracker
from .hooks import HookChain, HookResult, PreLandHook
from .lineage import DatasetFacet, LineageEmitter, RunEvent
from .provider import ApplyResult, PlanAction, Provider
from .quality import AnomalyResult, AnomalySignal, QualityGate, QualityResult, QualityRule
from .runner import RunContext, Runner, RunnerCapability, RunPlan, RunResult, RunState
from .schema import SchemaEvolutionDecision, SchemaFingerprint, SchemaPolicy
from .security import ImageSignatureVerifier, SovereigntyChecker
from .source import (
    AcquisitionMode,
    ConnectionSpec,
    DeliveryGuarantee,
    SinkSpec,
    SourceSpec,
)
from .state import Cursor, RunLock, StateStore, Watermark

__api_version__ = "1.0"

__all__ = [
    "__api_version__",
    # runner
    "Runner",
    "RunnerCapability",
    "RunResult",
    "RunContext",
    "RunPlan",
    "RunState",
    # provider
    "Provider",
    "PlanAction",
    "ApplyResult",
    # source / sink
    "SourceSpec",
    "SinkSpec",
    "AcquisitionMode",
    "ConnectionSpec",
    "DeliveryGuarantee",
    # state
    "StateStore",
    "Cursor",
    "Watermark",
    "RunLock",
    # lineage
    "LineageEmitter",
    "RunEvent",
    "DatasetFacet",
    # hooks
    "PreLandHook",
    "HookResult",
    "HookChain",
    # quality
    "QualityGate",
    "QualityResult",
    "QualityRule",
    "AnomalySignal",
    "AnomalyResult",
    # cost
    "CostTracker",
    "BudgetCap",
    "ChargebackTag",
    # catalog
    "CatalogRegistrar",
    "RegistrationResult",
    # schema
    "SchemaPolicy",
    "SchemaFingerprint",
    "SchemaEvolutionDecision",
    # security
    "ImageSignatureVerifier",
    "SovereigntyChecker",
]
