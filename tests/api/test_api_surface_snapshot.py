# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Public surface snapshot test for ``fluid_build.api``.

Locks the public extension contract. Any change to ``__all__`` or
``__api_version__`` requires explicit acknowledgement (update the
expected sets below) AND a version bump per SemVer with a 2-minor-version
deprecation window for any removal.
"""

from __future__ import annotations

import inspect

import fluid_build.api as api

EXPECTED_API_VERSION = "1.0"

EXPECTED_ALL = {
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
}


def test_api_version_constant() -> None:
    assert api.__api_version__ == EXPECTED_API_VERSION


def test_api_all_exact_match() -> None:
    actual = set(api.__all__)
    missing = EXPECTED_ALL - actual
    extra = actual - EXPECTED_ALL
    assert not missing and not extra, (
        f"Public surface drift detected.\n"
        f"  Missing: {sorted(missing)}\n"
        f"  Extra:   {sorted(extra)}\n"
        f"If intentional, update EXPECTED_ALL and bump fluid_build.api.__api_version__."
    )


def test_every_export_resolves() -> None:
    for name in api.__all__:
        assert hasattr(api, name), f"{name} declared in __all__ but not present on module"


def test_runner_protocol_signature() -> None:
    """Runner Protocol has the required class attributes and methods."""
    Runner = api.Runner
    annotations = getattr(Runner, "__annotations__", {})
    for attr in ("name", "declared_capabilities", "declared_modes"):
        assert attr in annotations, f"Runner.{attr} is part of the public Protocol"
    for method in ("plan", "run", "replay", "fingerprint"):
        assert hasattr(Runner, method), f"Runner.{method} is part of the public Protocol"


def test_provider_protocol_signature() -> None:
    Provider = api.Provider
    annotations = getattr(Provider, "__annotations__", {})
    for attr in ("name", "manages"):
        assert attr in annotations, f"Provider.{attr} is part of the public Protocol"
    for method in ("plan", "apply", "validate_sovereignty"):
        assert hasattr(Provider, method), f"Provider.{method} is part of the public Protocol"


def test_runner_capabilities_enum_values() -> None:
    """Locked enum values — adding is OK (version bump); removing breaks contract."""
    expected = {
        "full_refresh",
        "incremental_append",
        "incremental_dedup",
        "incremental_merge",
        "cdc",
        "streaming",
        "schema_discovery",
        "schema_evolution",
        "dlp_scan",
        "at_most_once",
        "at_least_once",
        "exactly_once",
    }
    actual = {c.value for c in api.RunnerCapability}
    missing = expected - actual
    assert not missing, f"RunnerCapability lost values (breaking change): {missing}"


def test_run_state_enum_values() -> None:
    expected = {"queued", "running", "succeeded", "partial", "failed", "cancelled", "archived"}
    actual = {s.value for s in api.RunState}
    assert expected <= actual, f"RunState lost values: {expected - actual}"


def test_acquisition_mode_enum_values() -> None:
    expected = {
        "full_refresh",
        "incremental_append",
        "incremental_dedup",
        "incremental_merge",
        "cdc",
        "streaming",
    }
    actual = {m.value for m in api.AcquisitionMode}
    assert expected <= actual


def test_delivery_guarantee_enum_values() -> None:
    actual = {g.value for g in api.DeliveryGuarantee}
    assert {"at_most_once", "at_least_once", "exactly_once"} <= actual


def test_schema_fingerprint_is_stable() -> None:
    """Sanity: the fingerprint hash is deterministic for the same column list."""
    cols = [
        api.SchemaFingerprint.__dataclass_fields__["columns"].type  # noqa: F841
    ]  # touch dataclass field to confirm shape
    from fluid_build.api.schema import SchemaColumn  # internal helper, not in __all__

    f1 = api.SchemaFingerprint.of(
        [SchemaColumn("id", "int", False), SchemaColumn("name", "str", True)]
    )
    f2 = api.SchemaFingerprint.of(
        [SchemaColumn("name", "str", True), SchemaColumn("id", "int", False)]
    )
    assert f1.digest == f2.digest, "Fingerprint must be column-order independent"


def test_no_internal_modules_leak_in_all() -> None:
    """Names in __all__ should not start with underscore."""
    assert all(not n.startswith("_") or n == "__api_version__" for n in api.__all__)


def test_modules_under_api_have_copyright_headers() -> None:
    import importlib
    import pkgutil

    import fluid_build.api as api_pkg

    for modinfo in pkgutil.walk_packages(api_pkg.__path__, prefix="fluid_build.api."):
        m = importlib.import_module(modinfo.name)
        src_file = inspect.getsourcefile(m)
        if src_file is None:
            continue
        with open(src_file) as fh:
            head = fh.read(500)
        assert "Copyright" in head, f"Missing copyright header in {modinfo.name}"
