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

"""SSRF hardening for ``HttpLineageEmitter``.

The OpenLineage HTTP emitter POSTs each run event (with an optional
Bearer token) to an operator-configured endpoint. The endpoint can be
sourced from a foreign contract, so before any POST the endpoint host
is run through the canonical ``_hostname_is_private`` gate. The request
itself uses ``httpx`` with ``follow_redirects=False``.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import respx

from fluid_build.api.lineage import RunEvent, RunEventType
from fluid_build.build_runners._lineage import HttpLineageEmitter

_PRIV = "fluid_build.build_runners._lineage._hostname_is_private"


def _event() -> RunEvent:
    return RunEvent(
        event_type=RunEventType.START,
        event_time="2026-05-15T00:00:00Z",
        run_id="01HXX",
        job_namespace="fluid",
        job_name="acquire.orders",
    )


@respx.mock
def test_emit_blocks_metadata_endpoint():
    """A lineage endpoint pointed at the cloud-metadata service must
    NOT receive the POST (which would carry the Bearer token)."""
    emitter = HttpLineageEmitter(endpoint="http://169.254.169.254/ingest", api_key="secret-token")
    route = respx.post(url__regex=r".*").mock(return_value=httpx.Response(200))
    emitter.emit(_event())
    assert not route.called, "POST to metadata endpoint must be refused"


@respx.mock
def test_emit_blocks_loopback_endpoint():
    emitter = HttpLineageEmitter(endpoint="http://127.0.0.1:5000/ingest")
    route = respx.post(url__regex=r".*").mock(return_value=httpx.Response(200))
    emitter.emit(_event())
    assert not route.called


@respx.mock
def test_emit_allows_public_endpoint():
    """A public endpoint is POSTed to normally."""
    emitter = HttpLineageEmitter(endpoint="https://lineage.example/ingest", api_key="tok")
    route = respx.post("https://lineage.example/ingest").mock(return_value=httpx.Response(202))
    with patch(_PRIV, return_value=False):
        emitter.emit(_event())
    assert route.called
    # Bearer header is attached for the public destination.
    assert route.calls[0].request.headers.get("Authorization") == "Bearer tok"


@respx.mock
def test_emit_does_not_follow_redirects():
    """``follow_redirects=False`` — the emitter must not chase a 30x
    (which could bounce the token-bearing POST to an internal host).
    A redirect response is treated as a non-2xx and soft-fails."""
    emitter = HttpLineageEmitter(endpoint="https://lineage.example/ingest")
    respx.post("https://lineage.example/ingest").mock(
        return_value=httpx.Response(307, headers={"Location": "http://169.254.169.254/"})
    )
    metadata_route = respx.post("http://169.254.169.254/").mock(return_value=httpx.Response(200))
    with patch(_PRIV, return_value=False):
        # Must not raise (soft-fail) and must not follow the redirect.
        emitter.emit(_event())
    assert not metadata_route.called


def test_emit_soft_fails_and_does_not_leak_url(caplog):
    """Emission errors are non-fatal and the failure log must not echo
    the endpoint URL (httpx error messages embed it)."""
    emitter = HttpLineageEmitter(endpoint="https://lineage.example/ingest?token=abc")
    with (
        patch(_PRIV, return_value=False),
        patch("httpx.Client.post", side_effect=httpx.ConnectError("boom")),
    ):
        with caplog.at_level("WARNING"):
            emitter.emit(_event())  # must not raise
    log_text = " ".join(r.getMessage() for r in caplog.records)
    assert "token=abc" not in log_text
    assert "ConnectError" in log_text
