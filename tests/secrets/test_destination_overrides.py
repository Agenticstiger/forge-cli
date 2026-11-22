# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A connector override may not move where a resolved secret is sent.

The destination policy clears the hosts in a connection block before its
secret is read. Debezium's ``properties.debezium.extra_config`` is merged into
the connector config after that, so a ``database.hostname`` (or a MongoDB
connection string) there would send the resolved secret to a host the
contract chose, past the check. Both the runner and ``fluid validate`` refuse it.
"""

from __future__ import annotations

import pytest

from fluid_build.build_runners.debezium.runner import build_connector_config
from fluid_build.secrets import secret_run
from fluid_build.secrets._env import EnvResolver
from fluid_build.secrets.contract import lint_secret_refs
from fluid_build.secrets.policy import AcquisitionSecretPolicyError

pytestmark = pytest.mark.unit

CONNECTION = {
    "host": "127.0.0.1",
    "user": "app",
    "secretRef": "env://FLUID_DBZ_TEST_PW",  # pragma: allowlist secret
}


def _build(extra):
    with secret_run("apply"):
        return build_connector_config(
            "postgres",
            connection=dict(CONNECTION),
            streams=["public.orders"],
            server_name="srv",
            snapshot_mode="initial",
            extra=extra,
        )


@pytest.mark.parametrize(
    "key", ["database.hostname", "mongodb.connection.string", "connection.url", "database.password"]
)
def test_the_runner_refuses_an_override_before_reading_the_secret(monkeypatch, key):
    def never(*_args, **_kwargs):
        raise AssertionError("the secret was read before the override was refused")

    monkeypatch.setattr(EnvResolver, "resolve", never)
    with pytest.raises(AcquisitionSecretPolicyError) as caught:
        _build({key: "attacker.example"})
    assert key in caught.value.what
    assert "attacker.example" not in caught.value.as_json()


def test_other_settings_still_pass_through(monkeypatch):
    monkeypatch.setenv("FLUID_DBZ_TEST_PW", "dbz-test-value-1234")
    cfg = _build({"snapshot.locking.mode": "none", "heartbeat.interval.ms": "1000"})
    assert cfg["snapshot.locking.mode"] == "none"
    assert cfg["heartbeat.interval.ms"] == "1000"
    assert cfg["database.hostname"] == "127.0.0.1"


def test_validate_refuses_it_too():
    contract = {
        "builds": [
            {
                "properties": {
                    "source": {"connection": dict(CONNECTION)},
                    "debezium": {"extra_config": {"database.hostname": "attacker.example"}},
                }
            }
        ]
    }
    findings = lint_secret_refs(contract, {})
    refused = [f for f in findings if f.error is not None]
    assert [f.site.pointer for f in refused] == ["builds[0].properties.debezium.extra_config"]
    assert isinstance(refused[0].error, AcquisitionSecretPolicyError)


def test_an_override_without_a_secret_is_not_this_policy_s_business():
    contract = {
        "builds": [
            {
                "properties": {
                    "source": {"connection": {"host": "127.0.0.1", "user": "app"}},
                    "debezium": {"extra_config": {"database.hostname": "elsewhere"}},
                }
            }
        ]
    }
    assert lint_secret_refs(contract, {}) == []
