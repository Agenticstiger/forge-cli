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

"""Resilience tests for the DataHub registrar.

Two layers:

* **Pure-function** tests of the ``_http_retry`` helper — transient
  classification, ``Retry-After`` parsing, backoff computation, and the
  retry loop's re-raise / exhaustion semantics. No HTTP.
* **respx-driven** tests of ``DataHubRegistrar`` end-to-end — a transient
  ``503`` / ``429`` self-heals, a non-transient ``400`` aborts on the
  first attempt, and a total outage returns a clean ``succeeded=False``
  result *without ever raising* (the "never block the pipeline on
  catalog unavailability" invariant).

These run in the light suite (no Docker / no live GMS) — the gated live
Quickstart coverage stays in ``tests/integration/test_catalog_datahub_*``.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from fluid_build.build_runners.catalog_registrars._http_retry import (
    DEFAULT_MAX_DELAY,
    RetryPolicy,
    compute_delay,
    is_transient_http_error,
    retry_after_seconds,
    run_with_retry,
)
from fluid_build.build_runners.catalog_registrars.datahub import DataHubRegistrar

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _status_error(status: int, headers: dict | None = None) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://datahub.test/entities?action=ingest")
    resp = httpx.Response(status, request=req, headers=headers or {})
    return httpx.HTTPStatusError(f"HTTP {status}", request=req, response=resp)


def _contract() -> dict:
    return {
        "id": "bronze.resilience",
        "name": "Resilience Test",
        "description": "retry hardening",
        "domain": "commerce",
        "version": "1.0.0",
        "metadata": {
            "layer": "Bronze",
            "productType": "SDP",
            "owner": {"team": "data-platform", "email": "dp@example.test"},
        },
        "tags": ["e2e"],
        "exposes": [
            {
                "exposeId": "orders",
                "binding": {"platform": "snowflake"},
                "contract": {"schema": [{"name": "id", "type": "STRING", "required": True}]},
            }
        ],
    }


def _payload():
    from fluid_build.api.catalog_publication import CatalogPublicationPayload

    return CatalogPublicationPayload.from_contract(_contract(), {})


# A fast registrar for respx tests: zero delay (no real sleeps), a fixed
# small attempt budget so exhaustion counts are deterministic.
def _registrar(max_attempts: int = 3) -> DataHubRegistrar:
    return DataHubRegistrar(
        base_url="https://datahub.test",
        retry_max_attempts=max_attempts,
        retry_base_delay=0.0,
        retry_max_delay=0.0,
    )


# ---------------------------------------------------------------------------
# Pure-function: transient classification
# ---------------------------------------------------------------------------


class TestIsTransientHttpError:
    @pytest.fixture
    def policy(self) -> RetryPolicy:
        return RetryPolicy()

    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
    def test_retryable_status_codes_are_transient(self, status, policy):
        assert is_transient_http_error(_status_error(status), policy) is True

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
    def test_client_errors_are_not_transient(self, status, policy):
        assert is_transient_http_error(_status_error(status), policy) is False

    def test_connect_error_is_transient(self, policy):
        assert is_transient_http_error(httpx.ConnectError("refused"), policy) is True

    def test_read_timeout_is_transient(self, policy):
        assert is_transient_http_error(httpx.ReadTimeout("slow"), policy) is True

    def test_programming_error_is_not_transient(self, policy):
        assert is_transient_http_error(ValueError("bug"), policy) is False


# ---------------------------------------------------------------------------
# Pure-function: Retry-After parsing
# ---------------------------------------------------------------------------


class TestRetryAfterSeconds:
    def test_integer_seconds(self):
        assert retry_after_seconds(_status_error(429, {"Retry-After": "7"})) == 7.0

    def test_http_date_form(self):
        # A date well in the future → a positive delta close to (huge).
        exc = _status_error(503, {"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"})
        secs = retry_after_seconds(exc)
        assert secs is not None and secs > 0

    def test_missing_header_returns_none(self):
        assert retry_after_seconds(_status_error(503)) is None

    def test_unparseable_header_returns_none(self):
        assert retry_after_seconds(_status_error(429, {"Retry-After": "soon"})) is None

    def test_non_status_error_returns_none(self):
        assert retry_after_seconds(httpx.ConnectError("x")) is None

    def test_negative_seconds_returns_none(self):
        assert retry_after_seconds(_status_error(429, {"Retry-After": "-5"})) is None


# ---------------------------------------------------------------------------
# Pure-function: backoff computation
# ---------------------------------------------------------------------------


class TestComputeDelay:
    def test_retry_after_wins_and_is_capped(self):
        policy = RetryPolicy(base_delay=1.0, max_delay=10.0, jitter=False)
        # Server said 999s; we cap to max_delay so a hostile value can't
        # stall the pipeline.
        assert compute_delay(1, policy, retry_after=999.0) == 10.0

    def test_retry_after_used_verbatim_no_jitter(self):
        policy = RetryPolicy(base_delay=1.0, max_delay=100.0, jitter=True)
        assert compute_delay(3, policy, retry_after=4.0) == 4.0

    def test_exponential_growth_without_jitter(self):
        policy = RetryPolicy(base_delay=1.0, max_delay=100.0, backoff_factor=2.0, jitter=False)
        assert compute_delay(1, policy) == 1.0
        assert compute_delay(2, policy) == 2.0
        assert compute_delay(3, policy) == 4.0

    def test_delay_capped_at_max(self):
        policy = RetryPolicy(base_delay=1.0, max_delay=5.0, backoff_factor=2.0, jitter=False)
        assert compute_delay(10, policy) == 5.0

    def test_jitter_stays_in_half_to_full_band(self):
        policy = RetryPolicy(base_delay=8.0, max_delay=100.0, backoff_factor=1.0, jitter=True)
        for _ in range(50):
            d = compute_delay(1, policy)
            assert 4.0 <= d <= 8.0  # half..full of base

    def test_default_max_delay_constant(self):
        assert RetryPolicy().max_delay == DEFAULT_MAX_DELAY


# ---------------------------------------------------------------------------
# Pure-function: the retry loop
# ---------------------------------------------------------------------------


class TestRunWithRetry:
    def test_success_first_try_no_sleep(self):
        slept: list[float] = []
        calls = {"n": 0}

        def _op():
            calls["n"] += 1
            return "ok"

        result = run_with_retry(_op, policy=RetryPolicy(), sleep=slept.append, description="t")
        assert result == "ok"
        assert calls["n"] == 1
        assert slept == []

    def test_retries_transient_then_succeeds(self):
        slept: list[float] = []
        attempts = {"n": 0}

        def _op():
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise _status_error(503)
            return "recovered"

        policy = RetryPolicy(max_attempts=4, base_delay=0.0, jitter=False)
        result = run_with_retry(_op, policy=policy, sleep=slept.append)
        assert result == "recovered"
        assert attempts["n"] == 3
        assert len(slept) == 2  # slept before attempts 2 and 3

    def test_non_transient_reraises_on_first_attempt(self):
        attempts = {"n": 0}

        def _op():
            attempts["n"] += 1
            raise _status_error(400)

        with pytest.raises(httpx.HTTPStatusError):
            run_with_retry(_op, policy=RetryPolicy(base_delay=0.0), sleep=lambda _: None)
        assert attempts["n"] == 1  # never retried

    def test_exhaustion_reraises_last_exception(self):
        attempts = {"n": 0}

        def _op():
            attempts["n"] += 1
            raise _status_error(503)

        policy = RetryPolicy(max_attempts=3, base_delay=0.0, jitter=False)
        with pytest.raises(httpx.HTTPStatusError):
            run_with_retry(_op, policy=policy, sleep=lambda _: None)
        assert attempts["n"] == 3  # exactly max_attempts

    def test_retry_after_drives_sleep_value(self):
        slept: list[float] = []
        attempts = {"n": 0}

        def _op():
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise _status_error(429, {"Retry-After": "2"})
            return "done"

        policy = RetryPolicy(max_attempts=3, base_delay=99.0, max_delay=100.0, jitter=True)
        run_with_retry(_op, policy=policy, sleep=slept.append)
        # The 429's Retry-After (2s) is honoured verbatim over the huge
        # computed backoff.
        assert slept == [2.0]

    def test_max_attempts_one_disables_retry(self):
        attempts = {"n": 0}

        def _op():
            attempts["n"] += 1
            raise _status_error(503)

        with pytest.raises(httpx.HTTPStatusError):
            run_with_retry(_op, policy=RetryPolicy(max_attempts=1), sleep=lambda _: None)
        assert attempts["n"] == 1


# ---------------------------------------------------------------------------
# Registrar end-to-end (respx) — the sink self-heals and never crashes
# ---------------------------------------------------------------------------


class TestRegistrarResilience:
    def test_transient_5xx_then_success_retries_and_publishes(self):
        with respx.mock(assert_all_called=False) as mock:
            snap = mock.post("https://datahub.test/entities?action=ingest").mock(
                side_effect=[
                    httpx.Response(503),
                    httpx.Response(503),
                    httpx.Response(200, json={"value": "ok"}),
                ]
            )
            mock.post("https://datahub.test/aspects?action=ingestProposal").mock(
                return_value=httpx.Response(200, json={"value": "ok"})
            )
            result = _registrar(max_attempts=3).register_payload(_payload())

        assert result.succeeded is True
        assert snap.call_count == 3  # 503, 503, 200

    def test_429_with_retry_after_self_heals(self):
        with respx.mock(assert_all_called=False) as mock:
            snap = mock.post("https://datahub.test/entities?action=ingest").mock(
                side_effect=[
                    httpx.Response(429, headers={"Retry-After": "0"}),
                    httpx.Response(200, json={"value": "ok"}),
                ]
            )
            mock.post("https://datahub.test/aspects?action=ingestProposal").mock(
                return_value=httpx.Response(200, json={"value": "ok"})
            )
            result = _registrar(max_attempts=3).register_payload(_payload())

        assert result.succeeded is True
        assert snap.call_count == 2

    def test_non_transient_400_aborts_without_retry_and_returns_clean_failure(self):
        with respx.mock(assert_all_called=False) as mock:
            snap = mock.post("https://datahub.test/entities?action=ingest").mock(
                return_value=httpx.Response(400, text="bad payload")
            )
            mock.post("https://datahub.test/aspects?action=ingestProposal").mock(
                return_value=httpx.Response(200, json={"value": "ok"})
            )
            # Must NOT raise — a metadata sink returns a failed result,
            # it never crashes the pipeline.
            result = _registrar(max_attempts=3).register_payload(_payload())

        assert result.succeeded is False
        assert snap.call_count == 1  # 400 is not retried

    def test_total_outage_returns_clean_failure_and_bounds_attempts(self):
        with respx.mock(assert_all_called=False) as mock:
            snap = mock.post("https://datahub.test/entities?action=ingest").mock(
                side_effect=httpx.ConnectError("connection refused")
            )
            # MCP path (structured-property bootstrap + domain) succeeds so
            # the outage is isolated to the dataset snapshot; the snapshot
            # then exhausts exactly ``max_attempts`` and the publish aborts.
            mock.post("https://datahub.test/aspects?action=ingestProposal").mock(
                return_value=httpx.Response(200, json={"value": "ok"})
            )
            result = _registrar(max_attempts=3).register_payload(_payload())

        assert result.succeeded is False
        assert result.error  # carries the failure reason
        assert snap.call_count == 3  # ConnectError retried up to the budget

    def test_full_gms_down_never_raises(self):
        """Even when *every* endpoint is dead, register_payload returns a
        failed result rather than propagating an exception."""
        with respx.mock(assert_all_called=False) as mock:
            mock.post("https://datahub.test/entities?action=ingest").mock(
                side_effect=httpx.ConnectError("down")
            )
            mock.post("https://datahub.test/aspects?action=ingestProposal").mock(
                side_effect=httpx.ConnectError("down")
            )
            # No exception here is the whole point.
            result = _registrar(max_attempts=2).register_payload(_payload())

        assert result.succeeded is False

    def test_unregister_retries_transient_delete(self):
        with respx.mock(assert_all_called=False) as mock:
            deletes = mock.post("https://datahub.test/entities?action=delete").mock(
                side_effect=[
                    httpx.Response(503),
                    httpx.Response(200, json={"value": "ok"}),
                    httpx.Response(200, json={"value": "ok"}),
                ]
            )
            result = _registrar(max_attempts=3).unregister("bronze.resilience", "orders")

        assert result.succeeded is True
        # dataset delete retried once (503→200), product delete once (200).
        assert deletes.call_count == 3


# ---------------------------------------------------------------------------
# Config surface
# ---------------------------------------------------------------------------


class TestRetryConfig:
    def test_default_attempts_mirror_datahub_emitter(self, monkeypatch):
        monkeypatch.delenv("FLUID_CATALOG_DATAHUB_MAX_RETRIES", raising=False)
        assert DataHubRegistrar(base_url="https://datahub.test").retry_max_attempts == 4

    def test_env_override_lowers_attempts(self, monkeypatch):
        monkeypatch.setenv("FLUID_CATALOG_DATAHUB_MAX_RETRIES", "1")
        assert DataHubRegistrar(base_url="https://datahub.test").retry_max_attempts == 1

    def test_env_override_applies_via_build_registrar(self, monkeypatch):
        from fluid_build.build_runners.catalog_registrars import build_registrar

        monkeypatch.setenv("DATAHUB_GMS_URL", "https://datahub.test")
        monkeypatch.setenv("FLUID_CATALOG_DATAHUB_MAX_RETRIES", "2")
        reg = build_registrar("datahub")
        assert reg is not None
        assert reg.retry_max_attempts == 2

    def test_malformed_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("FLUID_CATALOG_DATAHUB_MAX_RETRIES", "not-a-number")
        assert DataHubRegistrar(base_url="https://datahub.test").retry_max_attempts == 4
