# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Stage 8 (``fluid policy-apply``) survives a provider result that says ``message``.

``policy_apply.run`` logs the provider's result as the event payload,
``info(logger, "policy_apply_result", **res)``. The GCP provider's result
carries a ``message`` key, which collided with ``info()``'s own ``message``
parameter: ``TypeError: info() got multiple values for argument 'message'``,
exit 1, on every generated gcp pipeline (measured on 0.16.5 for all three
demo products). The bindings file below is the one ``fluid policy-compile``
wrote for the demo's bronze gcp overlay.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import pytest

from fluid_build.cli import _logging
from fluid_build.cli.policy_apply import run

pytestmark = pytest.mark.unit

_GCP_BINDINGS = {
    "bindings": [
        {
            "dataset": "demo_bronze",
            "principal": "group:data-platform@northwind.example",
            "project": "northwind-demo",
            "provider": "gcp",
            "resource_id": "northwind-demo.demo_bronze",
            "resource_type": "bigquery.dataset",
            "roles": ["roles/bigquery.dataViewer"],
        }
    ],
    "warnings": [],
}


def _args(path: Path, mode: str = "enforce") -> argparse.Namespace:
    return argparse.Namespace(bindings=str(path), mode=mode, provider=None, project=None)


@pytest.fixture
def events(caplog):
    caplog.set_level(logging.DEBUG, logger="test.policy_apply_message")
    return caplog


def _payloads(caplog, name: str):
    out = []
    for record in caplog.records:
        try:
            doc = json.loads(record.getMessage())
        except ValueError:
            continue
        if doc.get("message") == name:
            out.append(doc)
    return out


@pytest.mark.parametrize("mode", ["check", "enforce"])
def test_the_gcp_provider_result_is_logged_not_a_type_error(tmp_path, events, monkeypatch, mode):
    for var in ("GOOGLE_CLOUD_PROJECT", "FLUID_PROJECT", "GCLOUD_PROJECT", "FLUID_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    bindings = tmp_path / "bindings.json"
    bindings.write_text(json.dumps(_GCP_BINDINGS), encoding="utf-8")

    rc = run(_args(bindings, mode), logging.getLogger("test.policy_apply_message"))

    assert rc == 0
    (event,) = _payloads(events, "policy_apply_result")
    # The event keeps its name; the provider's own sentence is kept, renamed.
    assert event["status"] == "ok"
    assert "declaratively" in event["extra_message"]
    assert event["bindings"] == 1


class _Talkative:
    """A provider whose result names every envelope key."""

    name = "talkative"

    def apply_policy(self, data, mode="check"):
        return {
            "status": "ok",
            "message": "provider sentence",
            "time": "provider time",
            "level": "provider level",
            "name": "provider name",
        }


def test_any_provider_result_that_names_an_envelope_key_is_kept(tmp_path, events, monkeypatch):
    from fluid_build.cli import policy_apply

    monkeypatch.setattr(policy_apply, "build_provider", lambda *a, **k: _Talkative())
    bindings = tmp_path / "bindings.json"
    bindings.write_text(
        json.dumps({"bindings": [{"provider": "talkative", "roles": ["r"]}]}), encoding="utf-8"
    )

    assert run(_args(bindings), logging.getLogger("test.policy_apply_message")) == 0

    (event,) = _payloads(events, "policy_apply_result")
    assert event["level"] == "INFO"
    assert event["name"] == "fluid.cli"
    assert event["extra_message"] == "provider sentence"
    assert event["extra_time"] == "provider time"
    assert event["extra_level"] == "provider level"
    assert event["extra_name"] == "provider name"


@pytest.mark.parametrize("helper", ["info", "warn", "error"])
def test_every_structured_helper_takes_a_message_key(helper, caplog):
    caplog.set_level(logging.DEBUG, logger="test.policy_apply_message.helpers")
    log = logging.getLogger("test.policy_apply_message.helpers")
    getattr(_logging, helper)(log, "the_event", message="payload", logger="also payload")
    (record,) = caplog.records
    doc = json.loads(record.getMessage())
    assert doc["message"] == "the_event"
    assert doc["extra_message"] == "payload"
    assert doc["logger"] == "also payload"
