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

"""Pins for the opt-in web_search / web_fetch agent tools.

Covers:
  * the FLUID_AGENT_WEB_TOOLS gate (both tools ABSENT when unset,
    PRESENT when =1 — mirrors the dbt-MCP gate);
  * web_fetch SSRF safety — canned-HTML happy path, cloud-metadata
    rejection (request NEVER issued), DNS-rebind rejection, non-http(s)
    scheme rejection — all via respx-mocked httpx with the resolver
    stubbed through socket.getaddrinfo;
  * web_search provider plumbing — Tavily + Brave parse, provider
    selection, and the typed "not configured" result with no key;
  * the forge_copilot_tools integration (surface + route);
  * the redactor masks the Tavily / Brave key shapes.

No real network: httpx is mocked with respx and DNS is stubbed. Because
the SSRF guard rewrites every request URL to the pinned IP, respx routes
are matched by a host-agnostic URL regex (the same reconciliation the
federation backend tests use).
"""

from __future__ import annotations

import socket
from unittest.mock import patch

import pytest

httpx = pytest.importorskip("httpx")
respx = pytest.importorskip("respx")

from fluid_build.cli import forge_copilot_tools, forge_web_tools
from fluid_build.cli.forge_web_tools import (
    dispatch_web_tool,
    is_enabled,
    is_web_tool,
    web_tool_definitions,
)

_ON = {"FLUID_AGENT_WEB_TOOLS": "1"}


def _resolve_to(ip: str):
    """A socket.getaddrinfo side-effect that resolves every host to ``ip``."""

    def _fake(_host, _port, *_a, **_kw):
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 0, "", (ip, 0))]

    return _fake


# ── gate ────────────────────────────────────────────────────────────────────
class TestGate:
    def test_is_enabled_reads_env_flag(self):
        assert is_enabled({"FLUID_AGENT_WEB_TOOLS": "1"}) is True
        assert is_enabled({"FLUID_AGENT_WEB_TOOLS": "true"}) is True
        assert is_enabled({"FLUID_AGENT_WEB_TOOLS": "0"}) is False
        assert is_enabled({}) is False

    def test_is_web_tool_requires_name_and_enabled(self):
        assert is_web_tool("web_fetch", _ON) is True
        assert is_web_tool("web_search", _ON) is True
        assert is_web_tool("web_fetch", {}) is False  # disabled
        assert is_web_tool("propose_contract", _ON) is False  # not a web tool

    def test_definitions_empty_when_disabled(self):
        assert web_tool_definitions(env={}) == []

    def test_definitions_present_when_enabled(self):
        defs = web_tool_definitions(env=_ON)
        by_name = {d["name"]: d for d in defs}
        assert set(by_name) == {"web_search", "web_fetch"}
        # Schema derived from the Pydantic args models, unknown fields rejected.
        assert by_name["web_fetch"]["input_schema"]["properties"]["url"]["type"] == "string"
        assert by_name["web_fetch"]["input_schema"]["additionalProperties"] is False
        assert "query" in by_name["web_search"]["input_schema"]["properties"]


# ── get_tool_definitions integration ─────────────────────────────────────────
class TestToolListingIntegration:
    def test_absent_when_unset(self, monkeypatch):
        monkeypatch.delenv("FLUID_AGENT_WEB_TOOLS", raising=False)
        names = {t["name"] for t in forge_copilot_tools.get_tool_definitions()}
        assert "web_fetch" not in names
        assert "web_search" not in names

    def test_present_when_enabled(self, monkeypatch):
        monkeypatch.setenv("FLUID_AGENT_WEB_TOOLS", "1")
        names = {t["name"] for t in forge_copilot_tools.get_tool_definitions()}
        assert "web_fetch" in names
        assert "web_search" in names


# ── web_fetch — SSRF-safe fetch ──────────────────────────────────────────────
class TestWebFetch:
    def test_happy_path_returns_decoded_body(self):
        html = "<html><body><h1>Hello Forge</h1></body></html>"
        with patch(
            "fluid_build.util.safe_http.socket.getaddrinfo",
            side_effect=_resolve_to("93.184.216.34"),
        ):
            with respx.mock(assert_all_called=False) as router:
                route = router.get(url__regex=r"https?://[^/]+/page").mock(
                    return_value=httpx.Response(
                        200, html=html, headers={"content-type": "text/html; charset=utf-8"}
                    )
                )
                out = dispatch_web_tool("web_fetch", {"url": "https://example.com/page"})
        assert route.call_count == 1
        assert out["status"] == 200
        assert "Hello Forge" in out["text"]
        assert out["content_type"].startswith("text/html")
        assert out["truncated"] is False

    def test_metadata_url_rejected_and_never_requested(self):
        """The cloud-metadata endpoint is refused BEFORE any request."""
        with patch(
            "fluid_build.util.safe_http.socket.getaddrinfo",
            side_effect=_resolve_to("169.254.169.254"),
        ):
            with respx.mock(assert_all_called=False) as router:
                route = router.get(url__regex=r".*").mock(
                    return_value=httpx.Response(200, text="SHOULD NOT BE REACHED")
                )
                out = dispatch_web_tool(
                    "web_fetch", {"url": "http://169.254.169.254/latest/meta-data/"}
                )
        assert route.call_count == 0  # guard fired before the network
        assert out["error"] == "UnsafeURLError"

    def test_dns_rebind_public_name_private_ip_rejected(self):
        """A public-looking hostname that resolves to a private IP is
        rejected (defeats resolve-then-connect DNS rebind)."""
        with patch(
            "fluid_build.util.safe_http.socket.getaddrinfo",
            side_effect=_resolve_to("10.0.0.5"),
        ):
            with respx.mock(assert_all_called=False) as router:
                route = router.get(url__regex=r".*").mock(
                    return_value=httpx.Response(200, text="SHOULD NOT BE REACHED")
                )
                out = dispatch_web_tool("web_fetch", {"url": "https://totally-safe.example.com/x"})
        assert route.call_count == 0
        assert out["error"] == "UnsafeURLError"

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://example.com/x",
            "file:///etc/passwd",
            "data:text/plain,foo",
            "gopher://example.com/",
        ],
    )
    def test_non_http_scheme_rejected(self, url):
        out = dispatch_web_tool("web_fetch", {"url": url})
        assert out["error"] == "UnsafeURLError"

    def test_missing_url_typed_error(self):
        out = dispatch_web_tool("web_fetch", {"url": "   "})
        assert out["error"] == "InvalidArgs"

    def test_large_body_is_truncated(self):
        big = "A" * (forge_web_tools.MAX_FETCH_CHARS + 500)
        html = f"<html>{big}</html>"
        with patch(
            "fluid_build.util.safe_http.socket.getaddrinfo",
            side_effect=_resolve_to("93.184.216.34"),
        ):
            with respx.mock(assert_all_called=False) as router:
                router.get(url__regex=r"https?://[^/]+/big").mock(
                    return_value=httpx.Response(
                        200, html=html, headers={"content-type": "text/html"}
                    )
                )
                out = dispatch_web_tool("web_fetch", {"url": "https://example.com/big"})
        assert out["truncated"] is True
        assert len(out["text"]) == forge_web_tools.MAX_FETCH_CHARS


# ── web_search — provider plumbing ───────────────────────────────────────────
class TestWebSearch:
    def test_not_configured_when_no_key(self):
        out = dispatch_web_tool("web_search", {"query": "data mesh"}, env={})
        assert out["error"] == "SearchProviderNotConfigured"
        assert out["results"] == []

    def test_tavily_parsed_and_key_in_header(self):
        env = {"TAVILY_API_KEY": "tvly-testkey1234567890abcdef"}
        payload = {
            "results": [
                {"title": "T1", "url": "https://a.example/1", "content": "snip one"},
                {"title": "T2", "url": "https://a.example/2", "content": "snip two"},
            ]
        }
        with patch(
            "fluid_build.util.safe_http.socket.getaddrinfo",
            side_effect=_resolve_to("1.2.3.4"),
        ):
            with respx.mock(assert_all_called=False) as router:
                route = router.post(url__regex=r"https://[^/]+/search").mock(
                    return_value=httpx.Response(200, json=payload)
                )
                out = dispatch_web_tool("web_search", {"query": "data mesh"}, env=env)
        assert out["provider"] == "tavily"
        assert [r["title"] for r in out["results"]] == ["T1", "T2"]
        assert out["results"][0]["snippet"] == "snip one"
        # The key rode in the Authorization header (not the URL).
        sent = route.calls.last.request
        assert sent.headers["Authorization"] == "Bearer tvly-testkey1234567890abcdef"

    def test_brave_parsed_and_token_in_header(self):
        env = {"BRAVE_API_KEY": "BSAtestbravetoken1234567890"}
        payload = {
            "web": {
                "results": [
                    {"title": "B1", "url": "https://b.example/1", "description": "brave snip"},
                ]
            }
        }
        with patch(
            "fluid_build.util.safe_http.socket.getaddrinfo",
            side_effect=_resolve_to("1.2.3.4"),
        ):
            with respx.mock(assert_all_called=False) as router:
                route = router.get(url__regex=r"https://[^/]+/res/v1/web/search").mock(
                    return_value=httpx.Response(200, json=payload)
                )
                out = dispatch_web_tool("web_search", {"query": "kafka"}, env=env)
        assert out["provider"] == "brave"
        assert out["results"][0]["snippet"] == "brave snip"
        sent = route.calls.last.request
        assert sent.headers["X-Subscription-Token"] == "BSAtestbravetoken1234567890"

    def test_tavily_wins_over_brave_by_default(self):
        env = {"TAVILY_API_KEY": "tvly-k1234567890abcdef", "BRAVE_API_KEY": "BSAbrave1234567890"}
        assert forge_web_tools._select_search_provider(env) == "tavily"

    def test_forced_provider_honoured_when_key_present(self):
        env = {"TAVILY_API_KEY": "tvly-k1234567890abcdef", "BRAVE_API_KEY": "BSAbrave1234567890"}
        env2 = {**env, "FLUID_WEB_SEARCH_PROVIDER": "brave"}
        assert forge_web_tools._select_search_provider(env2) == "brave"

    def test_forced_provider_without_key_is_unconfigured(self):
        env = {"FLUID_WEB_SEARCH_PROVIDER": "tavily"}  # no TAVILY_API_KEY
        assert forge_web_tools._select_search_provider(env) is None

    def test_provider_error_returns_typed_no_leak(self):
        env = {"TAVILY_API_KEY": "tvly-secretkey1234567890"}
        with patch(
            "fluid_build.util.safe_http.socket.getaddrinfo",
            side_effect=_resolve_to("1.2.3.4"),
        ):
            with respx.mock(assert_all_called=False) as router:
                router.post(url__regex=r"https://[^/]+/search").mock(
                    return_value=httpx.Response(500, text="boom tvly-secretkey1234567890")
                )
                out = dispatch_web_tool("web_search", {"query": "x"}, env=env)
        assert out["results"] == []
        assert "error" in out
        # The key must not leak into the returned message.
        assert "tvly-secretkey1234567890" not in out["message"]


# ── dispatch_tool_call integration (the real wiring) ─────────────────────────
class TestDispatchIntegration:
    def test_routes_web_fetch_when_enabled(self, monkeypatch):
        monkeypatch.setenv("FLUID_AGENT_WEB_TOOLS", "1")
        html = "<html>ok</html>"
        with patch(
            "fluid_build.util.safe_http.socket.getaddrinfo",
            side_effect=_resolve_to("93.184.216.34"),
        ):
            with respx.mock(assert_all_called=False) as router:
                router.get(url__regex=r"https?://[^/]+/doc").mock(
                    return_value=httpx.Response(200, html=html)
                )
                out = forge_copilot_tools.dispatch_tool_call(
                    "web_fetch", {"url": "https://example.com/doc"}
                )
        assert "ok" in out["text"]

    def test_web_tool_unknown_when_disabled(self, monkeypatch):
        monkeypatch.delenv("FLUID_AGENT_WEB_TOOLS", raising=False)
        out = forge_copilot_tools.dispatch_tool_call("web_fetch", {"url": "https://example.com/"})
        assert "error" in out and "Unknown tool" in out["error"]


# ── redactor coverage ────────────────────────────────────────────────────────
class TestRedaction:
    def test_tavily_key_masked(self):
        from fluid_build.observability.secret_redactor import redact_secret_text

        out = redact_secret_text("using key tvly-dev-abcdef1234567890XYZ in header")
        assert "tvly-dev-abcdef1234567890XYZ" not in out
        assert "REDACTED" in out

    def test_brave_token_masked(self):
        from fluid_build.observability.secret_redactor import redact_secret_text

        out = redact_secret_text("X-Subscription-Token was BSAabcdef1234567890XYZ0123")
        assert "BSAabcdef1234567890XYZ0123" not in out
        assert "REDACTED" in out
