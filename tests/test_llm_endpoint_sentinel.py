# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``LlmConfig.endpoint`` holds only a real endpoint, never a telemetry sentinel.

litellm-backed providers used to resolve to ``litellm://<provider>/<model>``.
Consumers then dialled it: Ollama's ``api_base`` carried the sentinel, ``fluid
ai test`` POSTed to it, and forge copied it into ``args.llm_endpoint`` where it
read as an explicit endpoint on the next resolve. These tests drive the real
``resolve_llm_config`` -> ``build_request`` path so the class cannot return.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest import mock

import pytest

from fluid_build.llm.providers import (
    BUILTIN_LLM_PROVIDERS,
    LlmConfig,
    resolve_llm_config,
)

pytestmark = pytest.mark.unit

_OLLAMA_URL = "http://ollama.internal:11434"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("OLLAMA_HOST", "FLUID_LLM_ENDPOINT", "FLUID_LLM_PROVIDER", "FLUID_FORGE_AGENT"):
        monkeypatch.delenv(var, raising=False)


def _args(**kw):
    base = {"llm_provider": None, "llm_model": None, "llm_endpoint": None}
    base.update(kw)
    return SimpleNamespace(**base)


def _payload(config: LlmConfig) -> dict:
    _, payload = BUILTIN_LLM_PROVIDERS[config.provider].build_request(config, "sys", "usr")
    return payload


_KEYS = {
    "openai": {"OPENAI_API_KEY": "k"},
    "anthropic": {"ANTHROPIC_API_KEY": "k"},
    "gemini": {"GEMINI_API_KEY": "k"},
    "ollama": {},
}


@pytest.mark.parametrize("provider", sorted(_KEYS))
def test_endpoint_is_never_the_litellm_sentinel(provider):
    config = resolve_llm_config(_args(llm_provider=provider), _KEYS[provider])
    assert "litellm://" not in config.endpoint
    assert config.endpoint == ""
    assert "litellm://" not in config.redacted_endpoint


@pytest.mark.parametrize("provider", sorted(_KEYS))
def test_routing_config_never_carries_the_sentinel(provider):
    config = resolve_llm_config(_args(llm_provider=provider), _KEYS[provider])
    assert "litellm://" not in config.for_routing().endpoint


class TestRoutingKeepsAStatedEndpoint:
    """Routing calls go to the daemon the operator named, not a re-derived default."""

    _STATED = "http://127.0.0.1:9999"

    def test_flag_endpoint_reaches_routing_calls(self):
        config = resolve_llm_config(
            _args(llm_provider="ollama", llm_model="llama3", llm_endpoint=self._STATED), {}
        )
        routed = config.for_routing()
        assert routed.model == config.routing_model != config.model
        assert routed.endpoint == self._STATED
        assert _payload(routed)["api_base"] == self._STATED

    def test_env_endpoint_reaches_routing_calls(self):
        config = resolve_llm_config(
            _args(llm_provider="ollama", llm_model="llama3"),
            {"FLUID_LLM_ENDPOINT": self._STATED},
        )
        assert config.for_routing().endpoint == self._STATED

    def test_a_routing_endpoint_still_wins(self):
        config = resolve_llm_config(
            _args(
                llm_provider="ollama",
                llm_model="llama3",
                llm_endpoint=self._STATED,
                llm_routing_endpoint="http://127.0.0.1:9998",
            ),
            {},
        )
        assert config.for_routing().endpoint == "http://127.0.0.1:9998"

    def test_a_derived_endpoint_is_still_rederived(self):
        # Nothing stated: the provider's own default is asked again for the routing model.
        config = resolve_llm_config(_args(llm_provider="ollama", llm_model="llama3"), {})
        assert config.endpoint_explicit is False
        seen = []
        provider = BUILTIN_LLM_PROVIDERS["ollama"]
        with mock.patch.object(
            type(provider), "default_endpoint", lambda self, m, e: seen.append(m) or "derived"
        ):
            assert config.for_routing().endpoint == "derived"
        assert seen == [config.routing_model]

    def test_hosted_provider_without_an_endpoint_stays_empty(self):
        config = resolve_llm_config(_args(llm_provider="openai"), _KEYS["openai"])
        assert config.for_routing().endpoint == ""


def test_readiness_endpoint_is_not_a_fake_url():
    from fluid_build.llm.providers import check_llm_readiness

    readiness = check_llm_readiness({"FLUID_LLM_PROVIDER": "ollama"})
    assert "litellm://" not in (readiness.endpoint or "")


@pytest.mark.parametrize(
    "provider,expected", [("openai", "Provider default"), (None, "Not configured")]
)
def test_doctor_prints_no_fake_endpoint(capsys, provider, expected):
    from fluid_build.cli import doctor
    from fluid_build.llm.providers import LlmReadinessCheck

    readiness = LlmReadinessCheck(ready=bool(provider), provider=provider, endpoint="")
    doctor._print_copilot_readiness(readiness)

    out = capsys.readouterr().out
    assert expected in out
    assert "litellm://" not in out


class TestOllamaApiBase:
    def test_no_host_leaves_api_base_to_litellm(self):
        config = resolve_llm_config(_args(llm_provider="ollama"), {})
        assert "api_base" not in _payload(config)

    def test_ollama_host_becomes_api_base(self):
        config = resolve_llm_config(_args(llm_provider="ollama"), {"OLLAMA_HOST": _OLLAMA_URL})
        assert config.endpoint == _OLLAMA_URL
        assert _payload(config)["api_base"] == _OLLAMA_URL

    def test_schemeless_ollama_host_gets_http(self):
        # Ollama's own variable accepts ``host:port``; litellm needs a URL.
        config = resolve_llm_config(
            _args(llm_provider="ollama"), {"OLLAMA_HOST": "127.0.0.1:11500"}
        )
        assert _payload(config)["api_base"] == "http://127.0.0.1:11500"

    def test_flag_beats_ollama_host(self):
        config = resolve_llm_config(
            _args(llm_provider="ollama", llm_endpoint="http://localhost:9999"),
            {"OLLAMA_HOST": _OLLAMA_URL},
        )
        assert _payload(config)["api_base"] == "http://localhost:9999"

    def test_env_endpoint_beats_ollama_host(self):
        config = resolve_llm_config(
            _args(llm_provider="ollama"),
            {"FLUID_LLM_ENDPOINT": "http://localhost:9998", "OLLAMA_HOST": _OLLAMA_URL},
        )
        assert _payload(config)["api_base"] == "http://localhost:9998"

    def test_tool_request_uses_the_same_api_base(self):
        provider = BUILTIN_LLM_PROVIDERS["ollama"]
        bare = resolve_llm_config(_args(llm_provider="ollama"), {})
        hosted = resolve_llm_config(_args(llm_provider="ollama"), {"OLLAMA_HOST": _OLLAMA_URL})
        assert "api_base" not in provider.build_tool_request(bare, "s", [], [])[2]
        assert provider.build_tool_request(hosted, "s", [], [])[2]["api_base"] == _OLLAMA_URL

    def test_streaming_request_uses_the_same_api_base(self):
        provider = BUILTIN_LLM_PROVIDERS["ollama"]
        hosted = resolve_llm_config(_args(llm_provider="ollama"), {"OLLAMA_HOST": _OLLAMA_URL})
        assert provider.build_streaming_request(hosted, "s", "u")[2]["api_base"] == _OLLAMA_URL

    @pytest.mark.parametrize("host", [None, _OLLAMA_URL])
    def test_copying_the_endpoint_into_args_changes_nothing(self, host):
        # forge.py / init.py do ``args.llm_endpoint = llm_config.endpoint`` and
        # resolve again; that round trip must be the identity.
        env = {"OLLAMA_HOST": host} if host else {}
        args = _args(llm_provider="ollama")
        first = resolve_llm_config(args, env)
        args.llm_endpoint = first.endpoint
        second = resolve_llm_config(args, env)
        assert second.endpoint == first.endpoint
        assert _payload(second).get("api_base") == _payload(first).get("api_base")


# Forms of OLLAMA_HOST -> the URL both reads of it use. Same rules as
# ollama-python's ``_parse_host``: no scheme means http on 11434; an explicit
# scheme without a port means that scheme's default port; a path is kept.
_OLLAMA_HOST_FORMS = [
    ("myhost", "http://myhost:11434"),
    ("myhost:56789", "http://myhost:56789"),
    ("1.2.3.4", "http://1.2.3.4:11434"),
    (":56789", "http://127.0.0.1:56789"),
    ("http://myhost", "http://myhost:80"),
    ("https://myhost", "https://myhost:443"),
    ("http://myhost:56789", "http://myhost:56789"),
    ("https://myhost:56789", "https://myhost:56789"),
    ("myhost/", "http://myhost:11434"),
    ("myhost:56789/path/", "http://myhost:56789/path"),
    ("https://myhost:56789/path", "https://myhost:56789/path"),
    ("  myhost:56789  ", "http://myhost:56789"),
    ("[::1]", "http://[::1]:11434"),
    ("[::1]:56789", "http://[::1]:56789"),
    ("https://[::1]", "https://[::1]:443"),
]


class TestOllamaHostForms:
    @pytest.mark.parametrize("host, expected", _OLLAMA_HOST_FORMS)
    def test_base_url(self, host, expected):
        from fluid_build.llm.litellm_backend import _ollama_base_url

        assert _ollama_base_url({"OLLAMA_HOST": host}) == expected

    @pytest.mark.parametrize("env", [{}, {"OLLAMA_HOST": ""}, {"OLLAMA_HOST": "  "}])
    def test_unset_stays_empty_for_litellm_to_choose(self, env):
        from fluid_build.llm.litellm_backend import _ollama_base_url

        assert _ollama_base_url(env) == ""

    def test_an_unparseable_port_is_left_to_fail_at_the_dial(self):
        from fluid_build.llm.litellm_backend import _ollama_base_url

        assert _ollama_base_url({"OLLAMA_HOST": "myhost:abc"}) == "http://myhost:abc"

    @pytest.mark.parametrize("host, expected", _OLLAMA_HOST_FORMS)
    def test_the_model_list_dials_the_same_url(self, host, expected):
        from fluid_build.llm.litellm_backend import _list_ollama_models

        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")) as urlopen:
            assert _list_ollama_models({"OLLAMA_HOST": host}) is None
        assert urlopen.call_args.args[0].full_url == f"{expected}/api/tags"

    def test_the_model_list_defaults_to_localhost(self):
        from fluid_build.llm.litellm_backend import _list_ollama_models

        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")) as urlopen:
            _list_ollama_models({})
        assert urlopen.call_args.args[0].full_url == "http://localhost:11434/api/tags"

    def test_resolved_endpoint_and_api_base_use_the_bare_host_port(self):
        config = resolve_llm_config(_args(llm_provider="ollama"), {"OLLAMA_HOST": "myhost"})
        assert config.endpoint == "http://myhost:11434"
        assert _payload(config)["api_base"] == "http://myhost:11434"


class TestAiTestNeverDialsASentinel:
    """``fluid ai test`` probes litellm-backed providers through their own call path."""

    def _fake_litellm(self):
        fake = mock.MagicMock(spec=["completion", "completion_cost"])
        fake.completion.return_value = {
            "choices": [{"message": {"content": "FLUID_OK"}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
        }
        fake.completion_cost.return_value = 0.0
        return fake

    @pytest.mark.parametrize("provider", sorted(_KEYS))
    def test_smoke_call_goes_through_litellm_not_http(self, monkeypatch, provider):
        from fluid_build.cli.ai_setup import _run_ai_smoke_call

        fake = self._fake_litellm()
        monkeypatch.setitem(sys.modules, "litellm", fake)
        config = resolve_llm_config(_args(llm_provider=provider), _KEYS[provider])

        with mock.patch("httpx.Client", side_effect=AssertionError("raw HTTP POST")):
            text, usage, latency_ms = _run_ai_smoke_call(BUILTIN_LLM_PROVIDERS[provider], config)

        assert text == "FLUID_OK"
        assert usage["total_tokens"] == 9
        assert latency_ms >= 0
        kwargs = fake.completion.call_args.kwargs
        assert kwargs["max_tokens"] >= 1
        assert kwargs["timeout"] == config.timeout_seconds

    def test_smoke_call_unexpected_text_is_reported(self, monkeypatch):
        from fluid_build.cli.ai_setup import _run_ai_smoke_call
        from fluid_build.llm.providers import CopilotGenerationError

        fake = self._fake_litellm()
        fake.completion.return_value = {"choices": [{"message": {"content": "hi"}}]}
        monkeypatch.setitem(sys.modules, "litellm", fake)
        config = resolve_llm_config(_args(llm_provider="openai"), _KEYS["openai"])

        with pytest.raises(CopilotGenerationError) as exc_info:
            _run_ai_smoke_call(BUILTIN_LLM_PROVIDERS["openai"], config)
        assert exc_info.value.event == "ai_test_unexpected_response"

    @pytest.mark.parametrize(
        "event,exit_code",
        [
            ("copilot_llm_auth_failed", 3),
            ("copilot_llm_permission_denied", 3),
            ("copilot_litellm_request_failed", 5),
        ],
    )
    def test_litellm_errors_classify_to_the_documented_exit_codes(self, event, exit_code):
        from fluid_build.cli.ai_setup import _classify_ai_test_error

        assert _classify_ai_test_error(event, None) == exit_code

    @staticmethod
    def _exit_code_for(monkeypatch, error):
        """The exit code ``fluid ai test`` picks when the litellm call raises *error*."""
        from fluid_build.cli.ai_setup import (
            _classify_ai_test_error,
            _http_status_from_cause,
            _run_ai_smoke_call,
        )
        from fluid_build.llm.providers import CopilotGenerationError

        fake = mock.MagicMock(spec=["completion", "completion_cost"])
        fake.completion.side_effect = error
        monkeypatch.setitem(sys.modules, "litellm", fake)
        config = resolve_llm_config(_args(llm_provider="openai"), _KEYS["openai"])
        with pytest.raises(CopilotGenerationError) as exc_info:
            _run_ai_smoke_call(BUILTIN_LLM_PROVIDERS["openai"], config)
        exc = exc_info.value
        return _classify_ai_test_error(exc.event, _http_status_from_cause(exc))

    @pytest.mark.parametrize(
        "name, exit_code",
        [
            ("NotFoundError", 4),  # a model the provider does not have
            ("Timeout", 5),
            ("APIConnectionError", 5),
            ("RateLimitError", 5),
            ("ServiceUnavailableError", 5),
        ],
    )
    def test_litellm_not_found_is_a_resource_error_the_rest_stay_network(
        self, monkeypatch, name, exit_code
    ):
        litellm = pytest.importorskip("litellm")
        error = getattr(litellm, name)(message="boom", model="m", llm_provider="openai")
        assert self._exit_code_for(monkeypatch, error) == exit_code

    def test_a_404_from_an_unlisted_exception_class_is_also_a_resource_error(self, monkeypatch):
        class _Gone(Exception):
            status_code = 404

        assert self._exit_code_for(monkeypatch, _Gone("no such model")) == 4

    def test_an_error_without_a_status_stays_network(self, monkeypatch):
        assert self._exit_code_for(monkeypatch, RuntimeError("socket closed")) == 5

    @pytest.mark.parametrize("provider", sorted(_KEYS))
    def test_resolved_test_config_has_no_sentinel_and_validates(self, provider):
        from fluid_build.cli.ai_setup import _resolve_ai_test_config

        args = _args(llm_provider=provider, llm_timeout_seconds=None)
        with (
            mock.patch("fluid_build.cli.ai_setup._load_ai_config", return_value=None),
            mock.patch("fluid_build.cli.ai_setup._load_key_from_keyring", return_value="k"),
            mock.patch.dict("os.environ", _KEYS[provider], clear=True),
        ):
            config, error = _resolve_ai_test_config(args)

        assert error is None
        assert config is not None
        assert "litellm://" not in config.endpoint

    def test_a_litellm_url_is_not_a_valid_test_endpoint(self):
        from fluid_build.cli.ai_setup import _validate_ai_test_endpoint

        assert _validate_ai_test_endpoint("openai", "litellm://openai/gpt-4o") is not None

    def test_run_ai_test_never_posts_and_reports_no_fake_endpoint(self, monkeypatch):
        from fluid_build.cli.ai_setup import run_ai_test

        monkeypatch.setitem(sys.modules, "litellm", self._fake_litellm())
        args = _args(llm_provider="openai", llm_timeout_seconds=None, json=True)
        with (
            mock.patch("fluid_build.cli.ai_setup._load_ai_config", return_value=None),
            mock.patch("fluid_build.cli.ai_setup._load_key_from_keyring", return_value="k"),
            mock.patch.dict("os.environ", {"OPENAI_API_KEY": "k"}, clear=True),
            mock.patch("httpx.Client", side_effect=AssertionError("raw HTTP POST")),
            mock.patch("fluid_build.cli.ai_setup._check_ai_test_model_availability") as avail,
        ):
            avail.return_value = ("unavailable", None)
            code, report = run_ai_test(None, args)

        assert code == 0
        assert report["ok"] is True
        assert report["endpoint"] is None


@pytest.mark.parametrize(
    "provider,key_env,expected",
    [
        ("openai", "OPENAI_API_KEY", ("openai", "gpt-4.1-mini")),
        ("anthropic", "ANTHROPIC_API_KEY", ("anthropic", "claude-sonnet-4-6")),
        ("claude", "ANTHROPIC_API_KEY", ("anthropic", "claude-sonnet-4-6")),
        ("gemini", "GEMINI_API_KEY", ("gemini", "gemini-2.5-pro")),
    ],
)
def test_command_center_provider_and_model_are_unchanged(provider, key_env, expected):
    """Command Center runs ``fluid forge data-model from-intent --llm-provider X``
    with the key in env and no ``--llm-model``. Only the endpoint may change."""
    config = resolve_llm_config(_args(llm_provider=provider), {key_env: "k"})
    assert (config.provider, config.model) == expected
    assert config.endpoint == ""
