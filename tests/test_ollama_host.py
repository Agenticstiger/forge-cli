# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Every Ollama read agrees on which daemon, and any host is honoured.

Founder decision (2026-11-12): ``OLLAMA_HOST`` may name any machine, as the
ollama-python client allows; a project ``.env`` cannot set it. Before, the chat
call dialled a remote ``OLLAMA_HOST`` while detection, the default-model pick
and ``fluid ai setup`` ignored it with a warning and asked localhost instead.

A stated endpoint is where the chat call goes, so the model list, the
default-model pick and ``fluid ai test --endpoint`` ask that daemon too.

No test here touches the network: ``httpx.get`` and ``urlopen`` are patched.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest import mock

import pytest

from fluid_build.llm import providers
from fluid_build.llm.ollama_host import DEFAULT_OLLAMA_URL, ollama_base_url, ollama_daemon_url
from fluid_build.llm.providers import BUILTIN_LLM_PROVIDERS, LlmConfig, resolve_llm_config

pytestmark = pytest.mark.unit

_REMOTE = "http://gpu-box.lan:11434"
_LOCAL = "http://127.0.0.1:11434"
_TAGS = {"models": [{"name": "big:70b", "details": {"parameter_size": "70B"}}]}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in ("OLLAMA_HOST", "FLUID_LLM_ENDPOINT", "FLUID_LLM_PROVIDER", "FLUID_LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    providers.reset_llm_caches()
    yield
    providers.reset_llm_caches()


def _args(**kw):
    base = {"llm_provider": "ollama", "llm_model": None, "llm_endpoint": None}
    base.update(kw)
    return SimpleNamespace(**base)


def _httpx_tags():
    response = mock.MagicMock(status_code=200)
    response.json.return_value = _TAGS
    return mock.patch.object(providers.httpx, "get", return_value=response)


def _payload(config: LlmConfig) -> dict:
    _, payload = BUILTIN_LLM_PROVIDERS["ollama"].build_request(config, "sys", "usr")
    return payload


class TestOneReading:
    def test_a_stated_endpoint_beats_ollama_host(self):
        assert ollama_base_url({"OLLAMA_HOST": _LOCAL}, "gpu-box.lan") == _REMOTE

    def test_a_stated_endpoint_is_normalised_like_ollama_host(self):
        assert ollama_base_url({}, "gpu-box.lan:11434/") == _REMOTE

    def test_the_daemon_defaults_to_localhost(self):
        assert ollama_daemon_url({}) == DEFAULT_OLLAMA_URL
        assert ollama_daemon_url({"OLLAMA_HOST": "  "}) == DEFAULT_OLLAMA_URL


class TestAnyHostIsHonoured:
    def test_detection_dials_a_remote_ollama_host(self):
        with _httpx_tags() as get:
            assert providers.detect_ollama_available({"OLLAMA_HOST": _REMOTE}) is True
        assert get.call_args.args[0] == f"{_REMOTE}/api/version"

    def test_the_default_model_comes_from_the_remote_daemon(self):
        with _httpx_tags() as get:
            assert providers.resolve_ollama_model({"OLLAMA_HOST": _REMOTE}) == "big:70b"
        assert get.call_args.args[0] == f"{_REMOTE}/api/tags"

    def test_ai_setup_keeps_a_remote_host_without_a_warning(self, caplog):
        from fluid_build.cli.ai_setup import _normalize_ollama_host

        with caplog.at_level(logging.WARNING):
            assert _normalize_ollama_host(_REMOTE) == _REMOTE
        assert not caplog.records

    def test_ai_setup_blank_host_is_the_local_default(self):
        from fluid_build.cli.ai_setup import _normalize_ollama_host

        assert _normalize_ollama_host("") == DEFAULT_OLLAMA_URL

    def test_the_chat_call_and_the_model_pick_ask_the_same_daemon(self):
        with _httpx_tags() as get:
            config = resolve_llm_config(_args(), {"OLLAMA_HOST": "gpu-box.lan"})
        assert get.call_args.args[0] == f"{_REMOTE}/api/tags"
        assert config.model == "big:70b"
        assert _payload(config)["api_base"] == _REMOTE


class TestAStatedEndpointDrivesTheModelLookups:
    @pytest.mark.parametrize("how", ["flag", "env"])
    def test_the_default_model_is_picked_from_the_stated_endpoint(self, how):
        args = _args(llm_endpoint=_REMOTE) if how == "flag" else _args()
        env = {"OLLAMA_HOST": _LOCAL}
        if how == "env":
            env["FLUID_LLM_ENDPOINT"] = _REMOTE
        with _httpx_tags() as get:
            config = resolve_llm_config(args, env)
        assert get.call_args.args[0] == f"{_REMOTE}/api/tags"
        assert config.model == "big:70b"
        assert config.endpoint == _REMOTE

    def test_the_model_preflight_lists_the_stated_endpoint(self):
        env = {"OLLAMA_HOST": _LOCAL, "FLUID_LLM_MODEL_PREFLIGHT": "1"}
        args = _args(llm_endpoint=_REMOTE, llm_model="big:70b")
        with _httpx_tags() as get:
            config = resolve_llm_config(args, env)
        assert get.call_args.args[0] == f"{_REMOTE}/api/tags"
        assert not get.call_args.kwargs.get("follow_redirects", False)
        assert config.model == "big:70b"

    def test_a_bare_stated_endpoint_reaches_the_chat_call_as_a_url(self):
        config = resolve_llm_config(_args(llm_endpoint="gpu-box.lan", llm_model="m"), {"X": "1"})
        assert _payload(config)["api_base"] == _REMOTE

    def test_ai_test_lists_the_models_of_its_endpoint(self):
        from fluid_build.cli._ai_setup_test import _check_ai_test_model_availability

        config = LlmConfig(provider="ollama", model="big:70b", endpoint=_REMOTE, api_key=None)
        with _httpx_tags() as get:
            status, available = _check_ai_test_model_availability(
                BUILTIN_LLM_PROVIDERS["ollama"], config
            )
        assert get.call_args.args[0] == f"{_REMOTE}/api/tags"
        assert (status, available) == ("available", ["big:70b"])


def test_the_model_list_never_opens_a_non_http_url():
    from fluid_build.llm.litellm_backend import _list_ollama_models

    with mock.patch("httpx.get") as get:
        assert _list_ollama_models({}, "file:///etc/passwd") is None
    get.assert_not_called()


class TestCredentialsInTheAddress:
    def test_the_normaliser_keeps_user_and_password(self):
        # An Ollama behind a basic-auth proxy: ollama-python's _parse_host drops
        # the userinfo, fluid keeps it so the stated address still works.
        assert ollama_base_url({}, "https://u:p@proxy.lan/ollama/") == (
            "https://u:p@proxy.lan:443/ollama"  # pragma: allowlist secret
        )

    @pytest.mark.parametrize(
        "endpoint",
        [
            "http://u:hunter2@proxy.lan:11434",  # pragma: allowlist secret
            "http://hunter2@proxy.lan:11434",  # a token passed as a bare username
        ],
    )
    def test_doctor_masks_the_credentials(self, capsys, endpoint):
        from fluid_build.cli.doctor import _print_copilot_readiness
        from fluid_build.llm.providers import LlmReadinessCheck

        readiness = LlmReadinessCheck(
            ready=True, provider="ollama", model="m", endpoint=endpoint, auth_available=True
        )
        _print_copilot_readiness(readiness)
        out = capsys.readouterr().out
        assert "hunter2" not in out
        assert "proxy.lan:11434" in out

    def test_the_model_list_sends_them_as_basic_auth(self):
        # httpx turns userinfo into an Authorization header; urllib could not dial it.
        from fluid_build.llm.litellm_backend import _list_ollama_models

        with _httpx_tags() as get:
            assert _list_ollama_models({}, "https://u:p@proxy.lan/ollama") == ["big:70b"]
        assert get.call_args.args[0] == (
            "https://u:p@proxy.lan:443/ollama/api/tags"  # pragma: allowlist secret
        )

    def test_ai_setup_never_prints_them(self, monkeypatch):
        from fluid_build.cli import _ai_setup_ollama

        secret_host = "https://alice:s3cret@ollama.corp"  # pragma: allowlist secret
        monkeypatch.setenv("OLLAMA_HOST", secret_host)
        monkeypatch.setattr(_ai_setup_ollama, "_query_ollama_models", lambda host: [])
        monkeypatch.setattr("fluid_build.cli.forge_ui.ask_numbered_choice", lambda *a, **k: "skip")
        console = mock.MagicMock()
        assert _ai_setup_ollama._setup_ollama(console) is None
        printed = " ".join(str(c.args[0]) for c in console.print.call_args_list if c.args)
        assert "Couldn't reach Ollama" in printed
        assert "s3cret" not in printed and "alice" not in printed

    @pytest.mark.parametrize("plaintext_opt_in", [False, True])
    def test_ai_config_json_keeps_them_only_behind_the_plaintext_opt_in(
        self, tmp_path, monkeypatch, plaintext_opt_in
    ):
        from fluid_build.cli.ai_setup import _load_ai_config, _save_ai_config

        if plaintext_opt_in:
            monkeypatch.setenv("FLUID_ALLOW_PLAINTEXT_AI_SECRETS", "1")
        else:
            monkeypatch.delenv("FLUID_ALLOW_PLAINTEXT_AI_SECRETS", raising=False)
        secret_host = "https://alice:s3cret@ollama.corp:443"  # pragma: allowlist secret
        config_file = tmp_path / "ai_config.json"
        with (
            mock.patch("fluid_build.cli.ai_setup._CONFIG_FILE", config_file),
            mock.patch("fluid_build.cli.ai_setup._CONFIG_DIR", tmp_path),
        ):
            _save_ai_config("ollama", "m", ollama_host=secret_host, endpoint=secret_host)
            loaded = _load_ai_config()
        expected = secret_host if plaintext_opt_in else "https://ollama.corp:443"
        assert (loaded["ollama_host"], loaded["endpoint"]) == (expected, expected)
        assert ("s3cret" in config_file.read_text()) is plaintext_opt_in


class TestAiTestAcceptsAnyOllamaHost:
    @pytest.mark.parametrize("endpoint", [_REMOTE, "https://ollama.example.com"])
    def test_a_remote_ollama_endpoint_is_accepted(self, endpoint):
        from fluid_build.cli._ai_setup_test import _validate_ai_test_endpoint

        assert _validate_ai_test_endpoint("ollama", endpoint) is None

    @pytest.mark.parametrize(
        "endpoint",
        ["http://u:p@gpu-box.lan:11434", "gpu-box.lan:11434", "ftp://gpu-box.lan"],
    )
    def test_the_other_endpoint_rules_still_hold(self, endpoint):
        from fluid_build.cli._ai_setup_test import _validate_ai_test_endpoint

        assert _validate_ai_test_endpoint("ollama", endpoint) is not None


def test_an_upper_case_scheme_is_read_as_lower_case():
    assert ollama_base_url({}, "HTTP://gpu-box.lan") == "http://gpu-box.lan:80"


def test_redaction_ignores_the_scheme_case():
    from fluid_build.llm.providers import redact_endpoint

    assert redact_endpoint("HTTP://tok@gpu-box.lan:1") == "HTTP://***:***@gpu-box.lan:1"


class TestTieredStagesCallTheStatedEndpoint:
    def _resolve(self, config):
        from fluid_build.copilot.agents.base import BaseStageAgent, StageSession

        session = StageSession(store=mock.MagicMock(), llm_config=config, tiered=True)
        return BaseStageAgent(stage="s", tier="deep").resolve_model(session)

    def test_a_stated_endpoint_is_kept(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_HOST", _LOCAL)
        config = LlmConfig(
            provider="ollama", model="m", endpoint=_REMOTE, api_key=None, endpoint_explicit=True
        )
        assert self._resolve(config).endpoint == _REMOTE

    def test_an_unstated_endpoint_is_derived_again(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_HOST", "gpu-box.lan")
        config = LlmConfig(provider="ollama", model="m", endpoint="", api_key=None)
        assert self._resolve(config).endpoint == _REMOTE


def test_a_two_argument_plugin_still_preflights_without_an_endpoint(monkeypatch):
    # The ``endpoint`` keyword goes only with a stated endpoint.
    from fluid_build.llm.litellm_backend import LiteLLMProvider

    class _OldPlugin(LiteLLMProvider):
        def list_available_models(self, api_key, env):  # the pre-endpoint signature
            return ["gpt-x"]

    monkeypatch.setattr(providers, "get_llm_provider", lambda name: _OldPlugin("openai"))
    env = {"OPENAI_API_KEY": "k", "FLUID_LLM_MODEL_PREFLIGHT": "1"}  # pragma: allowlist secret
    config = resolve_llm_config(_args(llm_provider="openai", llm_model="gpt-x"), env)
    assert config.model == "gpt-x"


class TestSavedAddressesHoldNoCredentials:
    @pytest.mark.parametrize(
        "url, saved",
        [
            ("http://u:p@host:9", "http://host:9"),  # pragma: allowlist secret
            ("u:p@host:9", "host:9"),  # pragma: allowlist secret
            ("https://u:p@[::1]:9/x", "https://[::1]:9/x"),  # pragma: allowlist secret
            ("http://u:p@h?x=1", "http://h?x=1"),  # pragma: allowlist secret
            ("http://h?x=1@y", "http://h?x=1@y"),  # an "@" in the query is no userinfo
            ("http://host/path@x", "http://host/path@x"),  # nor one in the path
            ("http://h#frag@x", "http://h#frag@x"),
            ("", ""),
        ],
    )
    def test_the_address_is_saved_without_them(self, monkeypatch, caplog, url, saved):
        from fluid_build.cli._ai_setup_storage import _without_credentials

        monkeypatch.delenv("FLUID_ALLOW_PLAINTEXT_AI_SECRETS", raising=False)
        with caplog.at_level(logging.WARNING):
            assert _without_credentials(url) == saved
        assert all(":p@" not in record.getMessage() for record in caplog.records)

    def test_inline_setup_keeps_the_credentials_in_ollama_host(self, tmp_path, monkeypatch):
        # ai_config.json holds the address without them; the environment keeps them.
        from fluid_build.cli.ai_setup import _save_ai_config, run_ai_setup_inline

        monkeypatch.delenv("FLUID_ALLOW_PLAINTEXT_AI_SECRETS", raising=False)
        secret_host = "http://alice:s3cret@127.0.0.1:9"  # pragma: allowlist secret
        with (
            mock.patch("fluid_build.cli.ai_setup._CONFIG_FILE", tmp_path / "ai_config.json"),
            mock.patch("fluid_build.cli.ai_setup._CONFIG_DIR", tmp_path),
        ):
            _save_ai_config("ollama", "m", ollama_host=secret_host)
            monkeypatch.setenv("OLLAMA_HOST", secret_host)
            assert run_ai_setup_inline(None).provider == "ollama"
            assert providers.os.environ["OLLAMA_HOST"] == secret_host

            monkeypatch.delenv("OLLAMA_HOST")
            run_ai_setup_inline(None)
            assert providers.os.environ["OLLAMA_HOST"] == "http://127.0.0.1:9"


@pytest.mark.parametrize("failure", ["redirect", "not json"])
def test_the_model_list_is_unavailable_on_a_redirect_or_a_bad_body(failure):
    import httpx

    from fluid_build.llm.litellm_backend import _list_ollama_models

    response = mock.MagicMock(status_code=302 if failure == "redirect" else 200)
    if failure == "redirect":
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "302", request=mock.MagicMock(), response=response
        )
    else:
        response.json.side_effect = ValueError("not json")
    with mock.patch("httpx.get", return_value=response):
        assert _list_ollama_models({}, _REMOTE) is None
