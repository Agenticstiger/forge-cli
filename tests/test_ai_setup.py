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

"""Tests for fluid_build.cli.ai_setup — AI/LLM configuration."""

from __future__ import annotations

import argparse
import json
import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestSaveAndLoadConfig:
    def test_save_and_load_roundtrip(self, tmp_path):
        from fluid_build.cli.ai_setup import _load_ai_config, _save_ai_config

        config_file = tmp_path / "ai_config.json"
        with (
            patch("fluid_build.cli.ai_setup._CONFIG_FILE", config_file),
            patch("fluid_build.cli.ai_setup._CONFIG_DIR", tmp_path),
            patch("fluid_build.cli.ai_setup._save_key_to_keyring", return_value=True),
        ):
            assert _save_ai_config("openai", "gpt-4o", api_key="sk-test123")
            loaded = _load_ai_config()
            assert loaded is not None
            assert loaded["provider"] == "openai"
            assert loaded["model"] == "gpt-4o"
            assert "api_key" not in loaded

    def test_save_sets_permissions_600(self, tmp_path):
        from fluid_build.cli.ai_setup import _save_ai_config

        config_file = tmp_path / "ai_config.json"
        with (
            patch("fluid_build.cli.ai_setup._CONFIG_FILE", config_file),
            patch("fluid_build.cli.ai_setup._CONFIG_DIR", tmp_path),
            patch("fluid_build.cli.ai_setup._save_key_to_keyring", return_value=True),
        ):
            _save_ai_config("gemini", "gemini-2.5-flash", api_key="AIzaFake")
            mode = config_file.stat().st_mode
            assert mode & stat.S_IRUSR  # owner read
            assert mode & stat.S_IWUSR  # owner write
            assert not (mode & stat.S_IRGRP)  # no group read
            assert not (mode & stat.S_IROTH)  # no other read

    def test_save_plaintext_key_requires_explicit_opt_in(self, tmp_path):
        from fluid_build.cli.ai_setup import _load_ai_config, _save_ai_config

        config_file = tmp_path / "ai_config.json"
        with (
            patch("fluid_build.cli.ai_setup._CONFIG_FILE", config_file),
            patch("fluid_build.cli.ai_setup._CONFIG_DIR", tmp_path),
            patch("fluid_build.cli.ai_setup._save_key_to_keyring", return_value=False),
            patch.dict("os.environ", {}, clear=True),
        ):
            assert _save_ai_config("openai", "gpt-4o", api_key="sk-test123")
            loaded = _load_ai_config()
            assert loaded is not None
            assert loaded["provider"] == "openai"
            assert "api_key" not in loaded

    def test_save_plaintext_key_when_operator_opts_in(self, tmp_path):
        from fluid_build.cli.ai_setup import _load_ai_config, _save_ai_config

        config_file = tmp_path / "ai_config.json"
        with (
            patch("fluid_build.cli.ai_setup._CONFIG_FILE", config_file),
            patch("fluid_build.cli.ai_setup._CONFIG_DIR", tmp_path),
            patch("fluid_build.cli.ai_setup._save_key_to_keyring", return_value=False),
            patch.dict("os.environ", {"FLUID_ALLOW_PLAINTEXT_AI_SECRETS": "1"}, clear=True),
        ):
            assert _save_ai_config("openai", "gpt-4o", api_key="sk-test123")
            loaded = _load_ai_config()
            assert loaded is not None
            assert loaded["api_key"] == "sk-test123"

    def test_load_returns_none_when_no_file(self, tmp_path):
        from fluid_build.cli.ai_setup import _load_ai_config

        config_file = tmp_path / "nonexistent.json"
        with patch("fluid_build.cli.ai_setup._CONFIG_FILE", config_file):
            assert _load_ai_config() is None

    def test_load_returns_none_for_invalid_json(self, tmp_path):
        from fluid_build.cli.ai_setup import _load_ai_config

        config_file = tmp_path / "ai_config.json"
        config_file.write_text("not json", encoding="utf-8")
        with patch("fluid_build.cli.ai_setup._CONFIG_FILE", config_file):
            assert _load_ai_config() is None

    def test_load_returns_none_for_empty_provider(self, tmp_path):
        from fluid_build.cli.ai_setup import _load_ai_config

        config_file = tmp_path / "ai_config.json"
        config_file.write_text('{"provider": ""}', encoding="utf-8")
        with patch("fluid_build.cli.ai_setup._CONFIG_FILE", config_file):
            assert _load_ai_config() is None

    def test_clear_deletes_file(self, tmp_path):
        from fluid_build.cli.ai_setup import _clear_ai_config, _save_ai_config

        config_file = tmp_path / "ai_config.json"
        with (
            patch("fluid_build.cli.ai_setup._CONFIG_FILE", config_file),
            patch("fluid_build.cli.ai_setup._CONFIG_DIR", tmp_path),
        ):
            _save_ai_config("openai", "gpt-4o")
            assert config_file.exists()
            _clear_ai_config()
            assert not config_file.exists()

    def test_save_with_endpoint_and_ollama_host(self, tmp_path):
        from fluid_build.cli.ai_setup import _load_ai_config, _save_ai_config

        config_file = tmp_path / "ai_config.json"
        with (
            patch("fluid_build.cli.ai_setup._CONFIG_FILE", config_file),
            patch("fluid_build.cli.ai_setup._CONFIG_DIR", tmp_path),
        ):
            _save_ai_config(
                "ollama",
                "llama3.1",
                endpoint="http://localhost:11434/v1/chat/completions",
                ollama_host="http://localhost:11434",
            )
            loaded = _load_ai_config()
            assert loaded["ollama_host"] == "http://localhost:11434"
            assert loaded["endpoint"] == "http://localhost:11434/v1/chat/completions"


class TestDetectProviderFromApiKey:
    def test_detect_openai(self):
        from fluid_build.cli.forge_copilot_llm_providers import detect_provider_from_api_key

        assert detect_provider_from_api_key("sk-proj-abc123def456") == "openai"

    def test_detect_anthropic(self):
        from fluid_build.cli.forge_copilot_llm_providers import detect_provider_from_api_key

        assert detect_provider_from_api_key("sk-ant-api03-abc123") == "anthropic"

    def test_detect_gemini(self):
        from fluid_build.cli.forge_copilot_llm_providers import detect_provider_from_api_key

        assert detect_provider_from_api_key("AIzaSyA" + "x" * 30) == "gemini"

    def test_detect_unknown(self):
        from fluid_build.cli.forge_copilot_llm_providers import detect_provider_from_api_key

        assert detect_provider_from_api_key("some-random-key") is None

    def test_detect_empty(self):
        from fluid_build.cli.forge_copilot_llm_providers import detect_provider_from_api_key

        assert detect_provider_from_api_key("") is None
        assert detect_provider_from_api_key("   ") is None


class TestSetSessionEnv:
    def test_sets_openai_env(self):
        from fluid_build.cli.ai_setup import set_session_env

        with patch.dict("os.environ", {}, clear=False):
            set_session_env("openai", "sk-test")
            import os

            assert os.environ.get("OPENAI_API_KEY") == "sk-test"

    def test_sets_anthropic_env(self):
        from fluid_build.cli.ai_setup import set_session_env

        with patch.dict("os.environ", {}, clear=False):
            set_session_env("anthropic", "sk-ant-test")
            import os

            assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-test"

    def test_sets_gemini_env(self):
        from fluid_build.cli.ai_setup import set_session_env

        with patch.dict("os.environ", {}, clear=False):
            set_session_env("gemini", "AIzaTest")
            import os

            assert os.environ.get("GOOGLE_API_KEY") == "AIzaTest"


class TestCheckLlmReadiness:
    def test_ready_with_openai_env(self):
        from fluid_build.cli.forge_copilot_llm_providers import check_llm_readiness

        with patch("fluid_build.cli.ai_setup._load_ai_config", return_value=None):
            result = check_llm_readiness(
                {"OPENAI_API_KEY": "sk-test", "FLUID_LLM_PROVIDER": "openai"}
            )
        assert result.ready
        assert result.provider == "openai"
        assert result.auth_available

    def test_not_ready_without_keys(self):
        from fluid_build.cli.forge_copilot_llm_providers import check_llm_readiness

        # Patch every step in the resolution ladder so the real config
        # file isn't read, no ambient Ollama is picked up, and no
        # keyring entry leaks in.  The provider-precedence fix split
        # ``_infer_provider_from_env`` into explicit / keyring /
        # ambient helpers — ``check_llm_readiness`` calls them
        # individually so every test-time Ollama install must be
        # mocked at the ambient step.
        with (
            patch("fluid_build.cli.ai_setup._load_ai_config", return_value=None),
            patch("fluid_build.cli.ai_setup._CONFIG_FILE", Path("/nonexistent/ai_config.json")),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers._infer_provider_from_explicit_keys",
                return_value=None,
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers._infer_provider_from_keyring",
                return_value=None,
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers._infer_provider_from_ambient",
                return_value=None,
            ),
        ):
            result = check_llm_readiness({})
            assert not result.ready
            assert result.error is not None

    def test_not_ready_unknown_provider(self):
        from fluid_build.cli.forge_copilot_llm_providers import check_llm_readiness

        result = check_llm_readiness({"FLUID_LLM_PROVIDER": "nonexistent"})
        assert not result.ready

    def test_ready_with_gemini_api_key_alias(self):
        from fluid_build.cli.forge_copilot_llm_providers import check_llm_readiness

        with patch("fluid_build.cli.ai_setup._load_ai_config", return_value=None):
            result = check_llm_readiness(
                {"GEMINI_API_KEY": "AIzaTestAlias", "FLUID_LLM_PROVIDER": "gemini"}
            )
        assert result.ready
        assert result.provider == "gemini"
        assert result.auth_available


class TestShowAiStatus:
    def test_status_no_console(self):
        from fluid_build.cli.ai_setup import show_ai_status

        # Should not raise
        show_ai_status(None)

    @patch("fluid_build.cli.ai_setup.check_llm_readiness")
    def test_status_with_console_ready(self, mock_readiness):
        from fluid_build.cli.ai_setup import show_ai_status
        from fluid_build.cli.forge_copilot_llm_providers import LlmReadinessCheck

        mock_readiness.return_value = LlmReadinessCheck(
            ready=True,
            provider="openai",
            model="gpt-4o",
            auth_available=True,
        )
        console = MagicMock()
        show_ai_status(console)
        console.print.assert_called()


class TestAiTestCommand:
    def test_register_exposes_ai_test_args(self):
        from fluid_build.cli.ai_setup import register

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="cmd")
        register(subparsers)

        args = parser.parse_args(
            [
                "ai",
                "test",
                "--provider",
                "openai",
                "--model",
                "operator-model",
                "--timeout-seconds",
                "7",
            ]
        )

        assert args.cmd == "ai"
        assert args.ai_action == "test"
        assert args.llm_provider == "openai"
        assert args.llm_model == "operator-model"
        assert args.llm_timeout_seconds == 7

    def test_resolve_test_config_uses_saved_provider_and_keyring(self):
        from fluid_build.cli.ai_setup import _resolve_ai_test_config

        args = argparse.Namespace(
            llm_provider=None,
            llm_model=None,
            llm_endpoint=None,
            llm_timeout_seconds=None,
        )

        with (
            patch(
                "fluid_build.cli.ai_setup._load_ai_config",
                return_value={"provider": "openai", "model": "operator-model"},
            ),
            patch("fluid_build.cli.ai_setup._load_key_from_keyring", return_value="sk-test"),
            patch("fluid_build.cli.ai_setup.detect_ollama_available", return_value=False),
            patch.dict("os.environ", {}, clear=True),
        ):
            config, error = _resolve_ai_test_config(args)

        assert error is None
        assert config is not None
        assert config.provider == "openai"
        assert config.model == "operator-model"
        assert config.api_key == "sk-test"
        assert config.timeout_seconds == 30

    def test_resolve_test_config_falls_back_to_forge_keyring_namespace(self):
        from fluid_build.cli.ai_setup import _resolve_ai_test_config

        args = argparse.Namespace(
            llm_provider="openai",
            llm_model="operator-model",
            llm_endpoint=None,
            llm_timeout_seconds=None,
        )

        with (
            patch("fluid_build.cli.ai_setup._load_ai_config", return_value=None),
            patch("fluid_build.cli.ai_setup._load_key_from_keyring", return_value=None),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers._resolve_api_key",
                return_value="sk-test",
            ),
            patch.dict("os.environ", {}, clear=True),
        ):
            config, error = _resolve_ai_test_config(args)

        assert error is None
        assert config is not None
        assert config.api_key == "sk-test"

    def test_resolve_test_config_rejects_plaintext_cloud_endpoint(self):
        from fluid_build.cli.ai_setup import _resolve_ai_test_config

        args = argparse.Namespace(
            llm_provider="openai",
            llm_model=None,
            llm_endpoint="http://api.example.test/v1/chat/completions",
            llm_timeout_seconds=None,
        )

        with (
            patch("fluid_build.cli.ai_setup._load_ai_config", return_value=None),
            patch("fluid_build.cli.ai_setup._load_key_from_keyring", return_value="sk-test"),
            patch.dict("os.environ", {}, clear=True),
        ):
            config, error = _resolve_ai_test_config(args)

        assert config is None
        assert error is not None
        assert "HTTPS" in error

    def test_resolve_test_config_rejects_endpoint_embedded_credentials(self):
        from fluid_build.cli.ai_setup import _resolve_ai_test_config

        args = argparse.Namespace(
            llm_provider="openai",
            llm_model=None,
            llm_endpoint="https://user:secret@api.example.test/v1/chat/completions",
            llm_timeout_seconds=None,
        )

        with (
            patch("fluid_build.cli.ai_setup._load_ai_config", return_value=None),
            patch("fluid_build.cli.ai_setup._load_key_from_keyring", return_value="sk-test"),
            patch.dict("os.environ", {}, clear=True),
        ):
            config, error = _resolve_ai_test_config(args)

        assert config is None
        assert error is not None
        assert "must not embed credentials" in error

    def test_freeform_payload_disables_structured_outputs_and_caps_gemini(self):
        from fluid_build.cli.ai_setup import _with_freeform_ai_test_payload
        from fluid_build.cli.forge_copilot_llm_providers import LlmConfig

        class FakeGemini:
            def build_request(self, config, system_prompt, user_prompt):
                assert system_prompt
                assert user_prompt
                assert config.provider == "gemini"
                import os

                assert os.environ["FLUID_LLM_STRUCTURED_OUTPUTS"] == "0"
                return {"Content-Type": "application/json"}, {"generationConfig": {}}

        config = LlmConfig(
            provider="gemini",
            model="operator-model",
            endpoint="https://example.test",
            api_key="AIza-test",
        )

        with patch.dict("os.environ", {"FLUID_LLM_STRUCTURED_OUTPUTS": "1"}, clear=True):
            headers, payload = _with_freeform_ai_test_payload(FakeGemini(), config)
            import os

            assert os.environ["FLUID_LLM_STRUCTURED_OUTPUTS"] == "1"

        assert headers["Content-Type"] == "application/json"
        assert payload["generationConfig"]["maxOutputTokens"] == 256
        assert "thinkingConfig" not in payload["generationConfig"]

    def test_smoke_call_returns_fixed_success_token(self):
        from fluid_build.cli.ai_setup import _run_ai_smoke_call
        from fluid_build.cli.forge_copilot_llm_providers import LlmConfig

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"ok": True}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def post(self, *args, **kwargs):
                return FakeResponse()

        provider = MagicMock()
        provider.build_request.return_value = ({}, {})
        provider.extract_text.return_value = "FLUID_OK with extra text"
        provider.extract_usage.return_value = {
            "input_tokens": 1,
            "output_tokens": 1,
            "total_tokens": 2,
        }
        config = LlmConfig(
            provider="openai",
            model="operator-model",
            endpoint="https://example.test",
            api_key="sk-test",
        )

        with patch("fluid_build.cli.ai_setup.httpx.Client", FakeClient):
            text, usage, latency_ms = _run_ai_smoke_call(provider, config)

        assert text == "FLUID_OK"
        assert usage["total_tokens"] == 2
        assert isinstance(latency_ms, int) and latency_ms >= 0

    def test_smoke_call_network_error_does_not_echo_raw_url(self):
        from fluid_build.cli.ai_setup import _run_ai_smoke_call
        from fluid_build.cli.forge_copilot_llm_providers import (
            CopilotGenerationError,
            LlmConfig,
        )

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def post(self, *args, **kwargs):
                raise RuntimeError("should be replaced")

        class HttpxErrorClient(FakeClient):
            def post(self, *args, **kwargs):
                import httpx

                raise httpx.ConnectError("failed https://example.test/?key=secret")

        provider = MagicMock()
        provider.build_request.return_value = ({}, {})
        config = LlmConfig(
            provider="openai",
            model="operator-model",
            endpoint="https://example.test/?key=secret",
            api_key="sk-test",
        )

        with (
            patch("fluid_build.cli.ai_setup.httpx.Client", HttpxErrorClient),
            pytest.raises(CopilotGenerationError) as exc_info,
        ):
            _run_ai_smoke_call(provider, config)

        assert "secret" not in exc_info.value.message
        assert "ConnectError" in exc_info.value.message

    def test_model_availability_raises_when_configured_model_missing(self):
        from fluid_build.cli.ai_setup import _check_ai_test_model_availability
        from fluid_build.cli.forge_copilot_llm_providers import (
            CopilotGenerationError,
            LlmConfig,
        )

        provider = MagicMock()
        provider.list_available_models.return_value = ["other-model"]
        config = LlmConfig(
            provider="openai",
            model="operator-model",
            endpoint="https://example.test",
            api_key="sk-test",
        )

        with pytest.raises(CopilotGenerationError) as exc_info:
            _check_ai_test_model_availability(provider, config)
        assert "Configured openai model" in exc_info.value.message

    def test_run_ai_test_success(self):
        from fluid_build.cli.ai_setup import run_ai_test
        from fluid_build.cli.forge_copilot_llm_providers import LlmConfig

        args = argparse.Namespace()
        config = LlmConfig(
            provider="openai",
            model="operator-model",
            endpoint="https://api.openai.com/v1/chat/completions",
            api_key="sk-test",
        )

        with (
            patch("fluid_build.cli.ai_setup._resolve_ai_test_config", return_value=(config, None)),
            patch(
                "fluid_build.cli.ai_setup._check_ai_test_model_availability",
                return_value=("available", ["operator-model"]),
            ),
            patch(
                "fluid_build.cli.ai_setup._run_ai_smoke_call",
                return_value=(
                    "FLUID_OK",
                    {"input_tokens": 4, "output_tokens": 1, "total_tokens": 5},
                    137,
                ),
            ),
            patch("fluid_build.cli.console.cprint") as cprint,
        ):
            exit_code, report = run_ai_test(None, args)

        assert exit_code == 0
        assert report["ok"] is True
        assert report["exit_code"] == 0
        assert report["latency_ms"] == 137
        assert report["usage"] == {"input_tokens": 4, "output_tokens": 1, "total_tokens": 5}
        assert report["error"] is None
        printed = "\n".join(call.args[0] for call in cprint.call_args_list)
        assert "AI Provider Test: ready" in printed
        assert "Token usage: 4 input / 1 output / 5 total" in printed
        assert "Latency: 137 ms" in printed

    def test_run_ai_test_without_config_returns_config_exit_code(self):
        from fluid_build.cli.ai_setup import run_ai_test

        with (
            patch(
                "fluid_build.cli.ai_setup._resolve_ai_test_config",
                return_value=(None, "No AI provider configured."),
            ),
            patch("fluid_build.cli.console.cprint") as cprint,
        ):
            exit_code, report = run_ai_test(None, argparse.Namespace())

        assert exit_code == 2
        assert report["ok"] is False
        assert report["exit_code"] == 2
        assert report["error"]["code"] == "ai_test_no_provider"
        assert "No AI provider configured" in cprint.call_args.args[0]

    def test_run_ai_test_json_output_emits_stable_schema(self, capsys):
        from fluid_build.cli.ai_setup import run_ai_test
        from fluid_build.cli.forge_copilot_llm_providers import LlmConfig

        args = argparse.Namespace(json=True)
        config = LlmConfig(
            provider="gemini",
            model="gemini-2.5-flash",
            endpoint="https://generativelanguage.googleapis.com/v1beta/models/foo:generateContent",
            api_key="AIza-test",
        )

        with (
            patch("fluid_build.cli.ai_setup._resolve_ai_test_config", return_value=(config, None)),
            patch(
                "fluid_build.cli.ai_setup._check_ai_test_model_availability",
                return_value=("available", ["gemini-2.5-flash"]),
            ),
            patch(
                "fluid_build.cli.ai_setup._run_ai_smoke_call",
                return_value=(
                    "FLUID_OK",
                    {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
                    412,
                ),
            ),
        ):
            exit_code, report = run_ai_test(None, args)

        assert exit_code == 0
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        # Schema stability — every field a CI tool might depend on:
        assert parsed["schema_version"] == 1
        assert parsed["ok"] is True
        assert parsed["exit_code"] == 0
        assert parsed["provider"] == "gemini"
        assert parsed["model"] == "gemini-2.5-flash"
        assert parsed["model_availability"] == "available"
        assert parsed["live_call"] == "FLUID_OK"
        assert parsed["latency_ms"] == 412
        assert parsed["usage"]["total_tokens"] == 15
        assert parsed["error"] is None
        assert parsed["timestamp"].endswith("Z")
        # Mirrors the in-process report:
        assert parsed == report

    def test_run_ai_test_json_output_emits_error_on_failure(self, capsys):
        from fluid_build.cli.ai_setup import run_ai_test
        from fluid_build.cli.forge_copilot_llm_providers import (
            CopilotGenerationError,
            LlmConfig,
        )

        args = argparse.Namespace(json=True)
        config = LlmConfig(
            provider="openai",
            model="operator-model",
            endpoint="https://api.openai.com/v1/chat/completions",
            api_key="sk-test",
        )

        # Simulate 401 from preflight
        import httpx

        request = httpx.Request("GET", "https://api.openai.com/v1/models")
        response = httpx.Response(401, request=request)
        cause = httpx.HTTPStatusError("unauthorized", request=request, response=response)
        err = CopilotGenerationError(
            "ai_test_model_preflight_failed",
            "Could not list openai models (401).",
            suggestions=["Check the provider API key and account permissions"],
        )
        err.__cause__ = cause

        with (
            patch("fluid_build.cli.ai_setup._resolve_ai_test_config", return_value=(config, None)),
            patch(
                "fluid_build.cli.ai_setup._check_ai_test_model_availability",
                side_effect=err,
            ),
        ):
            exit_code, report = run_ai_test(None, args)

        assert exit_code == 3
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert parsed["ok"] is False
        assert parsed["exit_code"] == 3
        assert parsed["error"]["code"] == "ai_test_model_preflight_failed"
        assert parsed["error"]["message"] == "Could not list openai models (401)."
        assert "Check the provider API key" in parsed["error"]["suggestions"][0]
        assert parsed["latency_ms"] is None

    def test_classify_error_maps_status_codes(self):
        from fluid_build.cli.ai_setup import _classify_ai_test_error

        # Auth-class HTTP statuses
        for status in (401, 403, 407):
            assert _classify_ai_test_error("ai_test_request_failed", status) == 3
        # Other 4xx → resource (e.g., 404 model not found, 400 bad payload)
        for status in (400, 404, 422):
            assert _classify_ai_test_error("ai_test_request_failed", status) == 4
        # 5xx and 429 → network (transient)
        for status in (500, 502, 503, 504, 429):
            assert _classify_ai_test_error("ai_test_request_failed", status) == 5
        # No status (transport failure) → network
        assert _classify_ai_test_error("ai_test_network_error", None) == 5
        # Resource-class events
        for code in (
            "ai_test_model_unavailable",
            "ai_test_response_invalid",
            "ai_test_unexpected_response",
        ):
            assert _classify_ai_test_error(code, None) == 4

    def test_register_includes_json_flag(self):
        from fluid_build.cli.ai_setup import register

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="cmd")
        register(subparsers)

        args = parser.parse_args(["ai", "test", "--json"])
        assert args.json is True

        args_no_json = parser.parse_args(["ai", "test"])
        assert args_no_json.json is False

    def test_smoke_call_invalid_json_raises_response_invalid(self):
        from fluid_build.cli.ai_setup import _run_ai_smoke_call
        from fluid_build.cli.forge_copilot_llm_providers import (
            CopilotGenerationError,
            LlmConfig,
        )

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                raise ValueError("not JSON")

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def post(self, *args, **kwargs):
                return FakeResponse()

        provider = MagicMock()
        provider.build_request.return_value = ({}, {})
        config = LlmConfig(
            provider="openai",
            model="operator-model",
            endpoint="https://example.test",
            api_key="sk-test",
        )

        with (
            patch("fluid_build.cli.ai_setup.httpx.Client", FakeClient),
            pytest.raises(CopilotGenerationError) as exc_info,
        ):
            _run_ai_smoke_call(provider, config)

        assert exc_info.value.event == "ai_test_response_invalid"
        assert "JSON" in exc_info.value.message


class TestModelCatalogIntegrity:
    """Validate the catalog structure and provider-code consistency.

    These tests check STRUCTURE, not specific model names — so they
    stay green even after ``scripts/update_model_catalog.py`` changes
    the flagship/balanced selections.
    """

    def test_catalog_has_schema_version_2(self):
        from fluid_build.cli.forge_copilot_llm_providers import _load_model_catalog

        catalog = _load_model_catalog()
        assert catalog.get("schema_version") == 2

    def test_catalog_has_default_provider(self):
        from fluid_build.cli.forge_copilot_llm_providers import _load_model_catalog

        catalog = _load_model_catalog()
        assert catalog.get("default_provider") in ("gemini", "openai", "anthropic")

    def test_every_provider_has_flagship_and_balanced(self):
        from fluid_build.cli.forge_copilot_llm_providers import _load_model_catalog

        catalog = _load_model_catalog()
        for name in ("openai", "anthropic", "gemini", "ollama"):
            entry = catalog["providers"][name]
            assert "flagship" in entry, f"{name} missing flagship"
            assert "balanced" in entry, f"{name} missing balanced"
            assert "routing" in entry, f"{name} missing routing"

    def test_flagship_exists_in_models_list(self):
        """For non-ollama providers, the flagship model ID must appear
        in the models array so alias resolution works."""
        from fluid_build.cli.forge_copilot_llm_providers import _load_model_catalog

        catalog = _load_model_catalog()
        for name in ("openai", "anthropic", "gemini"):
            entry = catalog["providers"][name]
            flagship = entry["flagship"]
            model_ids = [m["id"] for m in entry.get("models", [])]
            assert (
                flagship in model_ids
            ), f"{name} flagship '{flagship}' not in models list {model_ids}"

    def test_class_defaults_match_catalog_flagship(self):
        """After _sync_provider_defaults_from_catalog runs, every
        provider's default_model must match the catalog's flagship."""
        from fluid_build.cli.forge_copilot_llm_providers import (
            BUILTIN_LLM_PROVIDERS,
            get_catalog_default,
        )

        for name in ("openai", "anthropic", "gemini", "ollama"):
            provider = BUILTIN_LLM_PROVIDERS[name]
            flagship = get_catalog_default(name)
            if flagship:
                assert provider.default_model == flagship, (
                    f"{name}: class default '{provider.default_model}' "
                    f"doesn't match catalog flagship '{flagship}'"
                )

    def test_capability_flags_are_booleans(self):
        from fluid_build.cli.forge_copilot_llm_providers import _load_model_catalog

        catalog = _load_model_catalog()
        for name, entry in catalog["providers"].items():
            for m in entry.get("models", []):
                caps = m.get("capabilities", {})
                for key in ("structured_output", "tool_use", "streaming"):
                    if key in caps:
                        assert isinstance(
                            caps[key], bool
                        ), f"{name}/{m['id']}.capabilities.{key} is not bool"

    def test_model_supports_structured_output_reads_catalog(self):
        from fluid_build.cli.forge_copilot_llm_providers import (
            model_supports_structured_output,
        )

        # OpenAI gpt-4.1 should support it (per catalog)
        assert model_supports_structured_output("openai", "gpt-4.1") is True
        # Gemini 2.5 Pro: structured output was flipped on (H4) — the
        # nested-freeform issue was resolved in the 2.5 line, and the
        # forge data-model pipeline requires strict JSON on every stage.
        assert model_supports_structured_output("gemini", "gemini-2.5-pro") is True
        # Unknown model → False
        assert model_supports_structured_output("openai", "unknown-model-xyz") is False

    def test_user_override_catalog_takes_precedence(self, tmp_path):
        """A user catalog at ~/.fluid/llm_models.json must be loaded
        instead of the bundled one."""
        import fluid_build.cli.forge_copilot_llm_providers as mod

        # Create the user catalog at the expected path
        fluid_dir = tmp_path / ".fluid"
        fluid_dir.mkdir()
        user_catalog = fluid_dir / "llm_models.json"
        user_catalog.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "default_provider": "openai",
                    "providers": {
                        "openai": {
                            "flagship": "test-flagship-model",
                            "balanced": "test-balanced-model",
                            "routing": "test-balanced-model",
                            "default": "test-flagship-model",
                            "models": [],
                        }
                    },
                }
            )
        )

        # Reset the catalog cache and patch Path.home to return tmp_path
        original_cache = mod._model_catalog_cache
        mod._model_catalog_cache = None
        try:
            with patch.object(Path, "home", return_value=tmp_path):
                catalog = mod._load_model_catalog()
                assert catalog["providers"]["openai"]["flagship"] == "test-flagship-model"
        finally:
            mod._model_catalog_cache = original_cache

    def test_get_catalog_tier_model(self):
        from fluid_build.cli.forge_copilot_llm_providers import get_catalog_tier_model

        # flagship tier returns the flagship model
        flagship = get_catalog_tier_model("openai", "flagship")
        assert flagship is not None

        # balanced tier returns a different (cheaper) model
        balanced = get_catalog_tier_model("openai", "balanced")
        assert balanced is not None

        # Both should be non-empty strings
        assert isinstance(flagship, str) and len(flagship) > 0
        assert isinstance(balanced, str) and len(balanced) > 0


class TestGeminiIsDefaultProvider:
    """Card 69da8d7e — Gemini is the default in every layer of the AI setup
    surface. Locked in across:

    1. ``llm_models.json`` catalog — ``default_provider == "gemini"``.
    2. ``_prompt_for_api_key`` numbered menu — Gemini's free path is the
       first option and ``default=1`` selects it.
    3. ``resolve_llm_config`` resolution chain — when no flag, env var, or
       env-var-inferred provider is present, the fallback is ``"gemini"``.

    The card was filed when OpenAI was the default everywhere; this suite
    prevents accidental regression to that state.
    """

    def test_catalog_default_provider_is_gemini(self):
        from fluid_build.cli.forge_copilot_llm_providers import _load_model_catalog

        catalog = _load_model_catalog()
        assert catalog.get("default_provider") == "gemini", (
            "llm_models.json default_provider regressed away from gemini — "
            "card 69da8d7e fix lost. Update both the catalog AND any UI copy."
        )

    def test_prompt_for_api_key_lists_gemini_first_with_default_one(self):
        """The picker source-of-truth: ``gemini`` is index 0 of the
        choices list, so ``default=1`` selects it. Gemini's free-tier
        sub-flow is now a follow-up prompt after the user picks
        ``gemini``, which keeps the top-level picker shorter and lists
        every provider via litellm."""
        import fluid_build.cli.ai_setup as mod

        src = Path(mod.__file__).read_text()
        # Locate the base_choices list — first row is the picker default.
        marker = "base_choices: List[Tuple[str, str]]"
        assert marker in src, "base_choices definition moved; update test"
        idx = src.index(marker)
        snippet = src[idx : idx + 1200]
        first_choice_pos = snippet.index('("')
        # The first tuple in base_choices must be the gemini row.
        assert snippet[first_choice_pos : first_choice_pos + 60].startswith('("gemini"'), (
            "AI-setup picker first option regressed away from "
            "Google Gemini — card 69da8d7e fix lost. Gemini must "
            "be the default pick because it's the cheapest path to "
            "world-class authoring (free tier covers most users)."
        )
        # And the default must point at index 1 (1-based) i.e. that first row.
        connect_idx = src.index('"How do you want to connect?"')
        connect_snippet = src[connect_idx : connect_idx + 400]
        assert "default=1" in connect_snippet, "ask_numbered_choice default= regressed"

    def test_resolve_llm_config_falls_back_to_gemini_when_unconfigured(self, monkeypatch):
        """With no flags, no FLUID_LLM_PROVIDER, no inferable provider env,
        and no API key already saved in the keyring, resolve_llm_config
        should pick gemini and surface the missing-API-key error rather
        than silently dropping to OpenAI."""
        from fluid_build.cli.forge_copilot_llm_providers import (
            CopilotGenerationError,
            resolve_llm_config,
        )

        # Strip every env var that could short-circuit the fallback.
        for key in (
            "FLUID_LLM_PROVIDER",
            "FLUID_LLM_MODEL",
            "FLUID_LLM_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "OLLAMA_HOST",
        ):
            monkeypatch.delenv(key, raising=False)

        args = argparse.Namespace(llm_provider=None, llm_model=None, llm_endpoint=None)

        with (
            patch(
                "fluid_build.cli.forge_copilot_llm_providers._infer_provider_from_keyring",
                return_value=None,
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers.detect_ollama_available",
                return_value=False,
            ),
            patch(
                "fluid_build.cli.forge_copilot_llm_providers._resolve_api_key",
                return_value=None,
            ),
        ):
            with pytest.raises(CopilotGenerationError) as exc_info:
                resolve_llm_config(args, environ={})

        # The error message — not just the event slug — must specifically
        # mention the gemini adapter, proving the fallback resolved to
        # gemini before the API-key check fired. ``CopilotGenerationError``
        # carries the user-facing detail string in ``message``.
        message = getattr(exc_info.value, "message", "") or ""
        assert "gemini" in message.lower(), message
        assert "openai" not in message.lower(), message

    def test_resolve_llm_config_explicit_keys_still_win_over_gemini_default(self, monkeypatch):
        """The Gemini-first fallback is the *no-signal* default. Operators
        who set OPENAI_API_KEY / ANTHROPIC_API_KEY / explicit
        ``--llm-provider openai`` must still get the provider they asked
        for. Otherwise the fallback would be a regression for everyone
        who explicitly opts into a different provider."""
        from fluid_build.cli.forge_copilot_llm_providers import _infer_provider_from_env

        for key in (
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "OLLAMA_HOST",
        ):
            monkeypatch.delenv(key, raising=False)

        # Explicit OpenAI key alone → openai wins.
        env = {"OPENAI_API_KEY": "sk-fake-test"}
        assert _infer_provider_from_env(env) == "openai"

        # Explicit Anthropic key alone → anthropic wins.
        env = {"ANTHROPIC_API_KEY": "sk-ant-fake-test"}
        assert _infer_provider_from_env(env) == "anthropic"

        # Explicit Gemini key alone → gemini wins (matches the default,
        # but exercises the explicit-key path, not the no-signal fallback).
        env = {"GEMINI_API_KEY": "AIza-fake-test"}
        assert _infer_provider_from_env(env) == "gemini"


class TestQueryOllamaModels:
    def test_returns_empty_when_httpx_not_installed(self):
        from fluid_build.cli.ai_setup import _query_ollama_models

        with patch.dict("sys.modules", {"httpx": None}):
            # Force reimport to hit ImportError
            import importlib

            import fluid_build.cli.ai_setup as mod

            # The function catches ImportError internally
            result = _query_ollama_models("http://localhost:11434")
            assert isinstance(result, list)
