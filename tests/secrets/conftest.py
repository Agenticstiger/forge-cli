# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Isolation for the secret-reference tests: no operator policy, no store, no cache.

The policy, the stores and the run-secrets file are all read from the process
environment, and the registry, the redactor and the Vault resolver keep
process-wide state. Every test here starts with none of it, so a variable set
in the developer's shell (a real ``VAULT_ADDR``, ``AWS_REGION``) can neither
widen what a test allows nor send a request anywhere.
"""

from __future__ import annotations

import pytest

#: Every variable the secret package reads.
SECRET_ENV = (
    "FLUID_SECRET_SCHEMES",
    "FLUID_SECRET_PREFIXES",
    "FLUID_SECRET_STAGES",
    "FLUID_ACQUISITION_SECRET_REF_ALLOWLIST",
    "FLUID_RUN_SECRETS_FILE",
    "FLUID_CREDENTIAL_STORE",
    "FLUID_VAULT_WRAPPED_TOKEN",
    "FLUID_VAULT_JWT_ROLE",
    "FLUID_VAULT_JWT",
    "FLUID_VAULT_JWT_FILE",
    "FLUID_VAULT_JWT_MOUNT",
    "BAO_ADDR",
    "VAULT_ADDR",
    "BAO_TOKEN",
    "VAULT_TOKEN",
    "BAO_NAMESPACE",
    "VAULT_NAMESPACE",
    "BAO_CACERT",
    "VAULT_CACERT",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
)


@pytest.fixture(autouse=True)
def _isolated_secret_env(monkeypatch, tmp_path):
    for name in SECRET_ENV:
        monkeypatch.delenv(name, raising=False)
    # ~/.vault-token is read from the home directory: never the developer's.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    from fluid_build.secrets import _vault, registry, run

    monkeypatch.setattr(registry, "_LOADED", {})
    monkeypatch.setattr(registry, "_REGISTERED", {})
    # The pack discovery is read once per process; a test may patch it away,
    # so clear the real one, captured before any test can replace it.
    discovery = registry._entry_points
    discovery.cache_clear()
    for name in ("FLUID_PLUGINS_ALLOWLIST", "FLUID_PLUGINS_BLOCKLIST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(_vault, "_UNWRAPPED", set())
    # The state lookups share outside every stage lives for the process: give
    # each test its own, and close it (revoking what it minted) afterwards.
    run._close_orphan()
    yield
    run._close_orphan()
    discovery.cache_clear()
