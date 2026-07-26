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
Bearer token) to an operator-configured endpoint. Before any POST the
endpoint host is resolved and gated; the request itself uses ``httpx``
with ``follow_redirects=False``.

Two gate modes, mirroring the sibling catalog registrars (which pass
``allow_private=True`` to ``safe_httpx_client`` for the same reason):

* default ``allow_private=True`` — the narrow link-local gate, so an
  internal Marquez / DataHub receiver works while the cloud
  instance-metadata shape is still refused;
* ``FLUID_OPENLINEAGE_ALLOW_PRIVATE=false`` — the broad
  ``_hostname_is_private`` gate (public endpoints only).
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import respx

from fluid_build.api.lineage import RunEvent, RunEventType
from fluid_build.build_runners._lineage import (
    HttpLineageEmitter,
    _allow_private_endpoints,
    resolve_lineage_emitter,
)

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
def test_emit_blocks_metadata_endpoint_in_strict_mode_too():
    """Link-local stays blocked on both sides of the ``allow_private`` switch."""
    emitter = HttpLineageEmitter(endpoint="http://169.254.169.254/ingest", allow_private=False)
    route = respx.post(url__regex=r".*").mock(return_value=httpx.Response(200))
    emitter.emit(_event())
    assert not route.called


@respx.mock
def test_emit_allows_loopback_receiver_by_default():
    """A Marquez / DataHub receiver on the host or the cluster network is
    the normal deployment, so it must be POSTed to.

    Regression: the emitter used the broad private-address gate
    unconditionally, so every realistic receiver (127.0.0.1, RFC1918, a
    ``docker compose`` service name) was refused and lineage silently
    never shipped.
    """
    emitter = HttpLineageEmitter(endpoint="http://127.0.0.1:5000/ingest")
    route = respx.post("http://127.0.0.1:5000/ingest").mock(return_value=httpx.Response(200))
    emitter.emit(_event())
    assert route.called


@respx.mock
def test_emit_blocks_loopback_endpoint_in_strict_mode():
    emitter = HttpLineageEmitter(endpoint="http://127.0.0.1:5000/ingest", allow_private=False)
    route = respx.post(url__regex=r".*").mock(return_value=httpx.Response(200))
    emitter.emit(_event())
    assert not route.called


def test_allow_private_defaults_on_and_is_operator_configurable(monkeypatch):
    monkeypatch.delenv("FLUID_OPENLINEAGE_ALLOW_PRIVATE", raising=False)
    assert _allow_private_endpoints() is True
    monkeypatch.setenv("FLUID_OPENLINEAGE_ALLOW_PRIVATE", "false")
    assert _allow_private_endpoints() is False
    monkeypatch.setenv("FLUID_OPENLINEAGE_ALLOW_PRIVATE", "1")
    assert _allow_private_endpoints() is True


def test_resolve_lineage_emitter_threads_allow_private(monkeypatch):
    monkeypatch.setenv("OPENLINEAGE_URL", "http://marquez:5000")
    monkeypatch.delenv("FLUID_OPENLINEAGE_URL", raising=False)
    monkeypatch.setenv("FLUID_OPENLINEAGE_ALLOW_PRIVATE", "false")
    emitter = resolve_lineage_emitter()
    assert isinstance(emitter, HttpLineageEmitter)
    assert emitter.allow_private is False


def test_refusal_is_surfaced_on_the_cli(monkeypatch, capsys):
    """The refusal must not be visible only on a logger nobody prints.

    Regression: during a normal ``fluid apply`` at default verbosity
    nothing about lineage reached the terminal, so an operator who set
    ``OPENLINEAGE_URL`` had no way to learn that nothing shipped.
    """
    import fluid_build.build_runners._lineage as lin

    monkeypatch.setattr(lin, "_WARNED", set())
    emitter = HttpLineageEmitter(endpoint="http://169.254.169.254/ingest")
    emitter.emit(_event())
    combined = capsys.readouterr()
    assert "OpenLineage emission skipped" in (combined.out + combined.err)


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
