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

"""Command Center reporting from a checkout whose ``fluid.config.yaml`` names
the endpoint, against a real loopback listener.

``fluid apply`` and ``fluid publish`` run in CI on code the operator may not
own. A Command Center endpoint committed in the project config is used only
with a credential from that same file, and its ``${VAR}`` placeholders are
not filled from the runner's environment. Operator config (environment,
``~/.fluid/config.yaml``) behaves as before.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

# Reuse the stub Command Center, the product and the stubbed tofu binary.
from tests.cli.test_apply_reports_to_command_center import (  # noqa: F401
    _ORG,
    _apply,
    _by,
    cc,
    product,
    tofu,
)

pytestmark = pytest.mark.unit

_ENV_KEY = "runner-key-not-a-secret"  # pragma: allowlist secret
_PROJECT_KEY = "project-key-not-a-secret"  # pragma: allowlist secret
_PROBE = "TOPSECRET42"


def _project_endpoint(product: Path, url: str, **auth) -> None:
    block = {"endpoint": f"{url}/x/${{PROBE_SECRET}}", "organization_id": _ORG}
    if auth:
        block["auth"] = {"type": "api_key", **auth}
    (product.parent / "fluid.config.yaml").write_text(
        yaml.safe_dump({"catalogs": {"fluid-command-center": block}}), encoding="utf-8"
    )


def _leaked(recorder) -> list:
    return [
        r
        for r in recorder.requests
        if _PROBE in r["path"] or _ENV_KEY in str(r["headers"]) or _PROBE in r["raw"]
    ]


def test_apply_does_not_send_the_runner_key_to_a_project_endpoint(
    product, tofu, cc, monkeypatch, capsys
):
    url, recorder = cc
    monkeypatch.setenv("PROBE_SECRET", _PROBE)
    monkeypatch.setenv("FLUID_API_KEY", _ENV_KEY)
    _project_endpoint(product, url)

    assert _apply(product) == 0

    assert _leaked(recorder) == []
    out = " ".join(capsys.readouterr().out.split())
    assert "run not reported" in out
    assert "FLUID_CC_ENDPOINT" in out
    assert url in out  # the origin is named, so the operator sees where
    assert "/x/" not in out


@pytest.mark.parametrize(
    ("auth", "env"),
    [
        pytest.param({"api_key": False}, "FLUID_API_KEY", id="api_key-false"),
        pytest.param({"api_key": 0}, "FLUID_API_KEY", id="api_key-zero"),
        pytest.param({"api_key": []}, "FLUID_API_KEY", id="api_key-empty-list"),
        pytest.param({"api_key": b""}, "FLUID_API_KEY", id="api_key-empty-binary"),
        pytest.param({"type": "bearer", "token": False}, "FLUID_BEARER_TOKEN", id="bearer-false"),
        pytest.param({"type": "bearer", "token": 0}, "FLUID_BEARER_TOKEN", id="bearer-zero"),
    ],
)
def test_apply_sends_no_runner_key_when_the_project_credential_is_not_a_string(
    product, tofu, cc, monkeypatch, capsys, auth, env
):
    url, recorder = cc
    monkeypatch.setenv(env, _ENV_KEY)
    _project_endpoint(product, url, **auth)

    assert _apply(product) == 0

    assert _leaked(recorder) == []
    assert recorder.requests == []
    out = " ".join(capsys.readouterr().out.split())
    assert "run not reported" in out
    assert "not sending" in out


def test_apply_reports_with_a_self_contained_project_config_literally(
    product, tofu, cc, monkeypatch
):
    url, recorder = cc
    monkeypatch.setenv("PROBE_SECRET", _PROBE)
    monkeypatch.setenv("FLUID_API_KEY", _ENV_KEY)
    _project_endpoint(product, url, api_key=_PROJECT_KEY)

    assert _apply(product) == 0

    assert _leaked(recorder) == []
    posts = _by(recorder, "POST")
    assert posts, "the self-contained project config still reports"
    for request in posts + _by(recorder, "PATCH"):
        assert request["headers"]["X-API-Key"] == _PROJECT_KEY


def test_apply_still_expands_the_user_config(product, tofu, cc, monkeypatch, capsys):
    url, recorder = cc
    monkeypatch.setenv("CC_URL", url)
    monkeypatch.setenv("CC_KEY", _ENV_KEY)
    user = Path.home() / ".fluid" / "config.yaml"
    user.parent.mkdir(parents=True, exist_ok=True)
    user.write_text(
        yaml.safe_dump(
            {
                "catalogs": {
                    "fluid-command-center": {
                        "endpoint": "${CC_URL}",
                        "organization_id": _ORG,
                        "auth": {"type": "api_key", "api_key": "${CC_KEY}"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    assert _apply(product) == 0

    (post,) = _by(recorder, "POST")
    assert post["path"] == "/api/v1/executions"
    assert post["headers"]["X-API-Key"] == _ENV_KEY
    assert "reported" in capsys.readouterr().out


def test_apply_refuses_plain_http_to_a_public_host(product, tofu, monkeypatch, capsys):
    monkeypatch.setenv("FLUID_CC_ENDPOINT", "http://cc.example.com/base")
    monkeypatch.setenv("FLUID_API_KEY", _ENV_KEY)
    monkeypatch.setattr(
        "fluid_build.observability.reporter._hostname_is_private", lambda host: False
    )
    from fluid_build.providers.catalogs.fluid_cc import FluidCommandCenterProvider

    monkeypatch.setattr(
        FluidCommandCenterProvider,
        "_list_organizations",
        lambda self: pytest.fail("the credential must not be sent over plain http"),
    )

    assert _apply(product) == 0

    out = " ".join(capsys.readouterr().out.split())
    assert "run not reported" in out
    assert "https" in out
    assert "http://cc.example.com" in out
    assert "/base" not in out


def test_publish_does_not_send_the_runner_key_to_a_project_endpoint(product, cc, monkeypatch):
    from fluid_build.cli.publish import publish_contract
    from fluid_build.config_manager import FluidConfig

    url, recorder = cc
    monkeypatch.setenv("PROBE_SECRET", _PROBE)
    monkeypatch.setenv("FLUID_API_KEY", _ENV_KEY)
    _project_endpoint(product, url)

    result = asyncio.run(
        publish_contract(product, "fluid-command-center", FluidConfig(), skip_health_check=True)
    )

    assert _leaked(recorder) == []
    assert recorder.requests == []
    assert result.success is False
    assert "FLUID_CC_ENDPOINT" in result.error
