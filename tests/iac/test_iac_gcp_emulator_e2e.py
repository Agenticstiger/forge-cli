# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""End-to-end: a real ``tofu apply`` of GCP-emitted ``.tf.json`` against
the gcloud Pub/Sub emulator.

The GCP analogue of ``test_iac_moto_e2e.py``: a FLUID contract compiled
to ``.tf.json`` by the GCP plugin, then provisioned by a real ``tofu``
init/plan/apply/destroy cycle creating emulated Pub/Sub resources — no
GCP project, no credentials.

HEAVY integration test — **CI integration stage only**. It needs
``tofu`` plus the gcloud Pub/Sub emulator running (``PUBSUB_EMULATOR_HOST``
set by ``gcloud beta emulators pubsub start``); the CI integration
workflow starts the emulator. It self-skips when either is absent, so it
is harmless in the light suite and in local runs.

Caveat — this is a CI *probe*, not a locally-proven pass. Unlike
moto+AWS (first-class endpoint override, high fidelity), the OpenTofu
``google`` provider's emulator support is partial: the Pub/Sub emulator
targets client-library / gRPC testing. This test establishes whether
``tofu apply`` against the emulator works for the cut-over GCP path.
"""

from __future__ import annotations

import json
import os
import socket
from typing import Any, Dict

import pytest

from fluid_build.iac import build_module, get_iac_plugin, runner
from fluid_build.iac.credentials import build_tofu_env

pytestmark = [
    pytest.mark.integration,
    pytest.mark.gcp,
    pytest.mark.provider,
    pytest.mark.emulated_heavy,
]

_EMULATOR_HOST = os.environ.get("PUBSUB_EMULATOR_HOST", "")


def _emulator_reachable() -> bool:
    """True when the gcloud Pub/Sub emulator is up at ``PUBSUB_EMULATOR_HOST``."""
    if not _EMULATOR_HOST:
        return False
    try:
        host, _, port = _EMULATOR_HOST.partition(":")
        with socket.socket() as sock:
            sock.settimeout(2)
            return sock.connect_ex((host or "localhost", int(port or "8085"))) == 0
    except Exception:  # noqa: BLE001
        return False


_SKIP = runner.tofu_path() is None or not _emulator_reachable()
_SKIP_REASON = "needs `tofu` + the gcloud Pub/Sub emulator (PUBSUB_EMULATOR_HOST)"

_PROJECT = "fluid-emulator"
_TOPIC = "fluid-emu-topic"
_SUBSCRIPTION = "fluid-emu-sub"
_CONTRACT = {
    "id": "demo.stream",
    "exposes": [
        {
            "exposeId": "events",
            "binding": {
                "platform": "gcp",
                "format": "pubsub_topic",
                "location": {"topic": _TOPIC, "subscription": _SUBSCRIPTION},
            },
        }
    ],
}


def _provider_override(endpoint_host: str) -> Dict[str, Any]:
    """A sidecar ``provider`` block aiming the google provider at the
    Pub/Sub emulator. ``tofu`` merges every ``*.tf.json`` in the dir, so
    this overlays endpoint config onto the plugin's portable output."""
    return {
        "provider": {
            "google": {
                "project": _PROJECT,
                # The emulator serves the v1 REST path over plain HTTP.
                "pubsub_custom_endpoint": f"http://{endpoint_host}/v1/",
            }
        }
    }


@pytest.mark.skipif(_SKIP, reason=_SKIP_REASON)
def test_pubsub_apply_destroy_against_emulator(tmp_path: Any) -> None:
    """contract -> .tf.json -> tofu init/plan/apply -> emulated Pub/Sub -> destroy."""
    (tmp_path / "main.tf.json").write_text(build_module(get_iac_plugin("gcp"), _CONTRACT))
    (tmp_path / "provider.tf.json").write_text(json.dumps(_provider_override(_EMULATOR_HOST)))
    workdir = str(tmp_path)
    env = build_tofu_env()
    # The emulator ignores credentials; a dummy static token stops the
    # provider from trying to resolve real Application Default Credentials.
    env["GOOGLE_OAUTH_ACCESS_TOKEN"] = "emulator-dummy-token"

    init = runner.tofu_init(workdir, env=env)
    assert init.ok, init.stderr or init.stdout

    plan = runner.tofu_plan(workdir, env=env)
    assert plan.ok, plan.stderr or plan.stdout
    assert runner.change_summary(plan)["add"] == 2  # topic + subscription

    try:
        applied = runner.tofu_apply(workdir, env=env)
        assert applied.ok, applied.stderr or applied.stdout
        assert runner.change_summary(applied)["add"] == 2
    finally:
        destroyed = runner.tofu_destroy(workdir, env=env)
        assert destroyed.ok, destroyed.stderr or destroyed.stdout
