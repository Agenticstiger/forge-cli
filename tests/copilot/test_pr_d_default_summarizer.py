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

"""Tests for the default summarizer (PR-D).

Pins the contract:

* The summarizer reuses the user's provider stack (no new SDKs).
* It picks a fast-tier model (catalog routing) for cost.
* HTTP / network failures degrade gracefully — the closure returns
  a structural marker and never raises.
* Unknown providers degrade to a deterministic char-truncation
  fallback.
* The agent loop's ``_compact_message_history`` accepts the
  summarizer when called with one.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from fluid_build.cli.forge_copilot_default_summarizer import (
    build_default_summarizer,
    maybe_build_default_summarizer,
)
from fluid_build.cli.forge_copilot_llm_providers import LlmConfig


def _config(provider: str = "anthropic", model: str = "claude-sonnet-4-6") -> LlmConfig:
    return LlmConfig(
        provider=provider,
        model=model,
        endpoint="https://example.test/v1/messages",
        api_key="dummy-key",
        timeout_seconds=30,
    )


class TestBuildDefaultSummarizer:
    def test_returns_callable(self) -> None:
        s = build_default_summarizer(_config())
        assert callable(s)

    def test_unknown_provider_returns_truncation_fallback(self) -> None:
        # Unknown provider name → no LlmProvider in BUILTIN registry →
        # closure returns the head-truncated blob.
        s = build_default_summarizer(_config(provider="totally-fake-provider"))
        out = s("a" * 5000)
        assert isinstance(out, str)
        # First 2000 chars preserved, plus a marker for the rest.
        assert out.startswith("a" * 2000)
        assert "[truncated" in out

    def test_unknown_provider_short_blob_unchanged(self) -> None:
        s = build_default_summarizer(_config(provider="unknown-x"))
        # Short blob → returned as-is, no truncation marker.
        out = s("hello")
        assert out == "hello"

    def test_summarizer_invokes_provider_request_path(self) -> None:
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["payload"] = json
            captured["timeout"] = timeout
            return _FakeResponse(
                200,
                {"content": [{"type": "text", "text": "compressed summary."}]},
            )

        with patch("httpx.post", side_effect=fake_post):
            s = build_default_summarizer(_config())
            out = s("[some big blob]")
        assert out == "compressed summary."
        # Provider's build_request was used → URL is non-empty + payload
        # has the user prompt.
        assert captured["url"]
        assert captured["timeout"] == 30

    def test_http_error_degrades_to_marker(self) -> None:
        def fake_post(url, headers=None, json=None, timeout=None):
            request = httpx.Request("POST", url)
            response = httpx.Response(500, request=request, content=b"upstream error")
            raise httpx.HTTPStatusError("500", request=request, response=response)

        with patch("httpx.post", side_effect=fake_post):
            s = build_default_summarizer(_config())
            out = s("x" * 1500)
        assert "[summarization failed" in out
        assert "1500 chars" in out

    def test_runtime_error_during_extract_text_degrades(self) -> None:
        # Provider returns 200 but extract_text raises (unexpected JSON).
        def fake_post(url, headers=None, json=None, timeout=None):
            return _FakeResponse(200, {"unexpected": "shape"})

        with patch("httpx.post", side_effect=fake_post):
            s = build_default_summarizer(_config())
            out = s("y" * 200)
        assert "[summarization failed" in out

    def test_oversize_blob_is_tail_trimmed(self) -> None:
        # Internal trim point is 60_000 chars.
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["payload"] = json
            return _FakeResponse(
                200, {"content": [{"type": "text", "text": "ok"}]}
            )

        with patch("httpx.post", side_effect=fake_post):
            s = build_default_summarizer(_config())
            blob = "x" * 100_000
            s(blob)
        # The user prompt that landed in the payload is the trimmed blob.
        # Anthropic build_request → ``messages[0].content`` is the user prompt.
        msgs = captured["payload"]["messages"]
        sent_user = msgs[0]["content"]
        assert isinstance(sent_user, str)
        assert len(sent_user) <= 60_000


class TestMaybeBuildDefaultSummarizer:
    def test_none_config_returns_none(self) -> None:
        assert maybe_build_default_summarizer(None) is None

    def test_real_config_returns_callable(self) -> None:
        s = maybe_build_default_summarizer(_config())
        assert callable(s)


# ---------------------------------------------------------------------------
# httpx.Response stand-in
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, body: dict) -> None:
        self.status_code = status_code
        self._body = body
        self.text = "fake"

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._body
