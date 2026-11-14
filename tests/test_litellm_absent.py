# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""What works, what degrades and what fails when litellm is not installed.

litellm left the base install in 0.28.0. The litellm-backed providers fail up
front with the install line and the keyless alternatives; the keyless coding
agents and template/blank forge keep working; the places
that only ever used litellm opportunistically (price table, token counting,
deprecation hints) degrade to their built-in fallbacks.
"""

from __future__ import annotations

import json

import pytest

from fluid_build._optional import MissingOptionalDependency
from tests.test_optional_deps import _hint, block, cli  # noqa: F401  (fixtures)

pytestmark = pytest.mark.unit

_ALTERNATIVES = (
    "Or author without it: --llm-provider claude-code (uses your Claude Code login) "
    "or --template starter."
)


class TestProviderGate:
    @pytest.mark.parametrize("name", ["openai", "anthropic", "gemini", "ollama", "bedrock"])
    def test_litellm_backed_provider_fails_with_hint_and_alternatives(
        self, block, name
    ):  # noqa: F811
        block("litellm")
        from fluid_build.llm.providers import get_llm_provider

        with pytest.raises(MissingOptionalDependency) as info:
            get_llm_provider(name)
        assert str(info.value) == (
            _hint(f"AI-assisted authoring (--llm-provider {name})", "litellm", "litellm")
            + "\n\n"
            + _ALTERNATIVES
        )
        assert info.value.event == "copilot_litellm_unavailable"
        assert info.value.exit_code == 1

    @pytest.mark.parametrize("name", ["claude-code", "codex", "cursor", "kiro"])
    def test_keyless_providers_resolve_without_litellm(self, block, name):  # noqa: F811
        block("litellm")
        from fluid_build.llm.providers import get_llm_provider

        assert get_llm_provider(name) is not None

    def test_resolve_llm_config_fails_before_any_key_check(self, block, monkeypatch):  # noqa: F811
        block("litellm")
        for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        from types import SimpleNamespace

        from fluid_build.llm.providers import resolve_llm_config

        with pytest.raises(MissingOptionalDependency):
            resolve_llm_config(SimpleNamespace(llm_provider="openai"))

    def test_keyless_config_resolves_without_litellm(self, block, monkeypatch):  # noqa: F811
        block("litellm")
        from types import SimpleNamespace

        from fluid_build.llm.providers import resolve_llm_config

        cfg = resolve_llm_config(SimpleNamespace(llm_provider="claude-code"))
        assert cfg.provider == "claude-code"


class TestCliSurface:
    def test_ai_forge_exits_1_with_the_hint_and_no_traceback(
        self, block, cli, monkeypatch
    ):  # noqa: F811
        block("litellm")
        rc, out = cli("forge", "--llm-provider", "openai", "--non-interactive")
        flat = " ".join(out.split())
        assert rc == 1, out
        assert "pip install 'data-product-forge[litellm]'" in flat
        assert _ALTERNATIVES in flat
        assert "Traceback" not in out

    def test_interactive_forge_fails_before_touching_the_keyring(
        self, block, cli, monkeypatch
    ):  # noqa: F811
        block("litellm")
        monkeypatch.delenv("FLUID_NONINTERACTIVE", raising=False)
        monkeypatch.setenv("FLUID_FORGE_NO_PICKER", "1")
        monkeypatch.setenv("FLUID_FORGE_NO_WELCOME", "1")
        calls: list[str] = []
        import fluid_build.cli.ai_setup as ai_setup

        monkeypatch.setattr(
            ai_setup, "run_ai_setup_inline", lambda *a, **k: calls.append("inline-setup")
        )
        for name in ("_load_key_from_keyring", "_save_key_to_keyring"):
            monkeypatch.setattr(ai_setup, name, lambda *a, _n=name, **k: calls.append(_n))
        try:
            import keyring
        except ImportError:
            pass
        else:
            for name in ("get_password", "set_password", "get_credential"):
                monkeypatch.setattr(
                    keyring, name, lambda *a, _n=name, **k: calls.append(f"keyring.{_n}")
                )
        rc, out = cli("forge", "--llm-provider", "openai")
        assert rc == 1, out
        assert "pip install 'data-product-forge[litellm]'" in " ".join(out.split())
        assert calls == []

    def test_ai_setup_fails_before_asking_for_a_key(self, block, cli, monkeypatch):  # noqa: F811
        block("litellm")
        import fluid_build.cli.ai_setup as ai_setup

        asked: list[str] = []
        monkeypatch.setattr(
            ai_setup, "run_ai_setup_interactive", lambda *a, **k: asked.append("wizard")
        )
        for extra in ((), ("--provider", "openai")):
            rc, out = cli("ai", "setup", *extra)
            flat = " ".join(out.split())
            assert rc == 1, out
            assert "pip install 'data-product-forge[litellm]'" in flat
            for keyless in ("claude-code", "codex", "cursor", "kiro", "--template"):
                assert keyless in flat
            assert "Traceback" not in out
        assert asked == []

    @pytest.mark.parametrize("sub", ["from-intent", "from-ddl"])
    def test_data_model_with_a_provider_prints_the_hint_once(
        self, block, cli, tmp_path, sub
    ):  # noqa: F811
        block("litellm")
        src = tmp_path / ("intent.yaml" if sub == "from-intent" else "a.sql")
        src.write_text(
            "name: sales\ngrain: order\n" if sub == "from-intent" else "CREATE TABLE t (id int);"
        )
        out_file = str(tmp_path / "out.yaml")
        args = ([sub, str(src)] if sub == "from-intent" else [sub, "--ddl", str(src)]) + [
            "--output",
            out_file,
            "--llm-provider",
            "openai",
        ]
        rc, out = cli("forge", "data-model", *args)
        flat = " ".join(out.split())
        assert rc == 1, out
        assert flat.count("pip install 'data-product-forge[litellm]'") == 1, out

    def test_a_provider_not_chosen_by_flag_is_not_called_a_flag(self, block):  # noqa: F811
        block("litellm")
        from types import SimpleNamespace

        from fluid_build.llm.providers import preflight_llm_provider

        args = SimpleNamespace(llm_provider=None, agent_loop=False)
        with pytest.raises(MissingOptionalDependency) as info:
            preflight_llm_provider(args, {"FLUID_LLM_PROVIDER": "ollama"})
        assert "(provider ollama)" in str(info.value)
        assert "--llm-provider ollama" not in str(info.value).split("\n")[0]

    def test_interactive_setup_offers_and_saves_no_litellm_provider(
        self, block, cli, monkeypatch  # noqa: F811
    ):
        block("litellm")
        import shutil

        import fluid_build.cli.ai_setup as ai_setup

        saved: list[tuple] = []
        offered: list[str] = []
        monkeypatch.setattr(ai_setup, "_save_ai_config", lambda *a, **k: saved.append(a))
        monkeypatch.setattr(ai_setup, "detect_ollama_available", lambda *a, **k: True)
        monkeypatch.setattr(
            ai_setup, "ask_confirmation", lambda *a, **k: offered.append("ask") or True
        )
        monkeypatch.setattr(
            ai_setup, "_prompt_for_api_key", lambda *a, **k: offered.append("key-prompt")
        )
        monkeypatch.setattr(shutil, "which", lambda *a, **k: None)
        monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
        for var in ("OLLAMA_HOST", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(ai_setup, "_load_ai_config", lambda *a, **k: None)
        monkeypatch.setattr(ai_setup, "_ai_setup_skipped", False)  # session state
        import io

        from rich.console import Console

        buf = io.StringIO()
        result = ai_setup.run_ai_setup_inline(Console(file=buf, width=250))
        assert result is None
        assert saved == [] and offered == []
        assert "pip install 'data-product-forge[litellm]'" in " ".join(buf.getvalue().split())

    def test_doctor_row_names_litellm_not_setup(self, block, monkeypatch):  # noqa: F811
        block("litellm")
        monkeypatch.setenv("FLUID_LLM_PROVIDER", "openai")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real-0000000000000000")
        from fluid_build.cli.doctor import _check_fluid_features

        _ok, checks = _check_fluid_features()
        row = next(c for c in checks if c["check"] == "Forge AI Copilot")
        assert "pip install 'data-product-forge[litellm]'" in row["details"]
        assert "fluid ai setup" not in row["details"]

    def test_ai_status_keeps_the_hint(self, block, cli, monkeypatch):  # noqa: F811
        block("litellm")
        monkeypatch.setenv("FLUID_LLM_PROVIDER", "openai")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real-0000000000000000")
        rc, out = cli("ai", "status")
        assert "pip install 'data-product-forge[litellm]'" in " ".join(out.split()), out

    def test_template_forge_works_without_litellm(self, block, cli, tmp_path):  # noqa: F811
        block("litellm")
        rc, out = cli(
            "forge",
            "--template",
            "starter",
            "--non-interactive",
            "--yes",
            "--target-dir",
            str(tmp_path / "proj"),
        )
        assert "Traceback" not in out, out
        assert "litellm" not in out.lower() or "pip install" not in out, out


class TestDegradations:
    def test_price_table_is_used_when_litellm_is_absent(self, block):  # noqa: F811
        block("litellm")
        from fluid_build.copilot import cost

        model = "claude-sonnet-4-6"
        in_price, out_price = cost.MODEL_PRICES_USD[model]
        got = cost._price_for("anthropic", model, 1_000_000, 2_000_000)
        assert got == pytest.approx(in_price + 2 * out_price)
        assert cost._resolve_per_million_rate("anthropic", model) == (in_price, out_price)

    def test_unknown_model_price_is_none_not_an_error(self, block):  # noqa: F811
        block("litellm")
        from fluid_build.copilot import cost

        assert cost._price_for("openai", "model-nobody-has-heard-of", 10, 10) is None

    def test_token_count_falls_back_to_the_char_heuristic(self, block, monkeypatch):  # noqa: F811
        block("litellm")
        monkeypatch.delenv("FLUID_TOKEN_COUNTER", raising=False)
        from fluid_build.copilot.agents import token_budget as tb

        text = "select * from orders where id = 1" * 20
        assert tb.count_tokens(text, provider="openai", model="gpt-4o") == tb.estimate_tokens(text)
        assert tb._lookup_litellm_max_input_tokens("gpt-4o") is None


class TestCodingAgentWithoutLitellm:
    def test_invoke_blocking_runs_with_litellm_blocked(self, block, monkeypatch):  # noqa: F811
        block("litellm")
        from fluid_build.cli import forge_copilot_coding_agent as cap
        from fluid_build.llm.providers import LlmConfig, get_llm_provider

        structured = {"recommended_template": "starter", "contract": {"id": "x"}}
        stdout = json.dumps({"structured_output": structured, "total_cost_usd": 0.01})
        calls = []

        def fake_run(argv, *, stdin, timeout, cwd, env):
            calls.append(argv)
            return (0, stdout, "")

        monkeypatch.setattr(cap.shutil, "which", lambda b: f"/usr/local/bin/{b}")
        monkeypatch.setattr(cap, "_run_agent", fake_run)

        provider = get_llm_provider("claude-code")
        cfg = LlmConfig(
            provider="claude-code",
            model="claude-code",
            endpoint="coding-agent://claude-code",
            api_key=None,
        )
        result = provider.invoke_blocking(cfg, "SYS", "USER")
        assert json.loads(result) == structured
        assert len(calls) == 1


def test_readiness_is_not_ready_when_a_key_is_set_but_litellm_is_absent(block):  # noqa: F811
    block("litellm")
    from fluid_build.llm.providers import check_llm_readiness

    result = check_llm_readiness({"OPENAI_API_KEY": "sk-test1234567890abcdef"})
    assert result.ready is False
    assert result.auth_available is True
    assert "pip install 'data-product-forge[litellm]'" in (result.error or "")


class TestNoKeyringBeforeTheHint:
    """Without litellm, nothing reads a credential store or probes a port first."""

    @staticmethod
    def _spy(monkeypatch) -> list[str]:
        calls: list[str] = []
        import fluid_build.cli.ai_setup as ai_setup

        for name in ("_load_key_from_keyring", "_save_key_to_keyring", "detect_ollama_available"):
            monkeypatch.setattr(ai_setup, name, lambda *a, _n=name, **k: calls.append(_n))
        try:
            import keyring
        except ImportError:
            pass
        else:
            for name in ("get_password", "set_password", "get_credential"):
                monkeypatch.setattr(
                    keyring, name, lambda *a, _n=name, **k: calls.append(f"keyring.{_n}")
                )
        monkeypatch.setattr(
            "fluid_build.llm.providers._infer_provider_from_env",
            lambda *a, **k: calls.append("infer") or None,
        )
        return calls

    def test_preflight_with_no_selector_fails_before_inference(
        self, block, monkeypatch
    ):  # noqa: F811
        block("litellm")
        calls = self._spy(monkeypatch)
        from types import SimpleNamespace

        from fluid_build.llm.providers import preflight_llm_provider

        args = SimpleNamespace(llm_provider=None, agent_loop=False)
        with pytest.raises(MissingOptionalDependency) as info:
            preflight_llm_provider(args, {})
        assert "pip install 'data-product-forge[litellm]'" in str(info.value)
        assert _ALTERNATIVES in str(info.value)
        assert calls == []

    def test_preflight_with_a_keyless_selector_still_passes(self, block, monkeypatch):  # noqa: F811
        block("litellm")
        from types import SimpleNamespace

        from fluid_build.llm.providers import preflight_llm_provider

        preflight_llm_provider(SimpleNamespace(llm_provider="claude-code"), {})
        preflight_llm_provider(SimpleNamespace(llm_provider=None), {"FLUID_FORGE_AGENT": "codex"})

    def test_init_handoff_prints_the_hint_and_touches_no_credential_store(
        self, block, monkeypatch, tmp_path, capsys  # noqa: F811
    ):
        block("litellm")
        calls = self._spy(monkeypatch)
        import logging
        from types import SimpleNamespace
        from unittest.mock import patch

        import fluid_build.cli.ai_setup as ai_setup
        from fluid_build.cli.init import _offer_first_forge

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(ai_setup, "_load_ai_config", lambda *a, **k: None)
        monkeypatch.setattr(
            ai_setup, "_offer_import_from_other_clis", lambda *a, **k: calls.append("import")
        )
        monkeypatch.setattr(
            ai_setup, "_prompt_for_api_key", lambda *a, **k: calls.append("key-prompt")
        )
        for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OLLAMA_HOST"):
            monkeypatch.delenv(var, raising=False)
        args = SimpleNamespace(target_dir=str(tmp_path), name=None, provider=None)
        with (
            patch("fluid_build.cli.init._is_ai_configured", return_value=False),
            patch("fluid_build.cli.init.RICH_AVAILABLE", True),
            patch("rich.prompt.Confirm.ask", return_value=True),
            patch("fluid_build.cli.forge.run") as forge_run,
        ):
            _offer_first_forge(args, logging.getLogger("test"))
        out = " ".join((lambda c: c.out + c.err)(capsys.readouterr()).split())
        assert "pip install 'data-product-forge[litellm]'" in out
        assert "--llm-provider claude-code" in out
        assert calls == []
        forge_run.assert_not_called()


class TestLeanKeylessAutoPick:
    """Without litellm, a non-interactive run may still author with an installed agent."""

    @staticmethod
    def _lean(block, monkeypatch, tmp_path, *, tty=False, ollama=False, saved=None):  # noqa: F811
        import stat
        import sys
        from types import SimpleNamespace

        from fluid_build.llm import providers

        block("litellm")
        bindir = tmp_path / "bin"
        bindir.mkdir()
        exe = bindir / "claude"
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
        monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: tty))
        monkeypatch.setattr(providers, "detect_ollama_available", lambda env: ollama)
        monkeypatch.setattr(providers, "_get_api_key_from_keyring", lambda name: None)
        monkeypatch.setattr("fluid_build._ai_config_shared.load_ai_config", lambda *a, **k: saved)
        return {"PATH": str(bindir)}

    @staticmethod
    def _args(**kw):
        from types import SimpleNamespace

        return SimpleNamespace(**{"llm_provider": None, "agent_loop": False, **kw})

    def test_agent_on_path_passes_the_preflight(self, block, monkeypatch, tmp_path):  # noqa: F811
        from fluid_build.llm.providers import _provider_name_from, preflight_llm_provider

        env = self._lean(block, monkeypatch, tmp_path)
        preflight_llm_provider(self._args(), env)
        assert _provider_name_from(self._args(), env) == "claude-code"

    def test_running_ollama_still_fails_fast(self, block, monkeypatch, tmp_path):  # noqa: F811
        from fluid_build.llm.providers import preflight_llm_provider

        env = self._lean(block, monkeypatch, tmp_path, ollama=True)
        with pytest.raises(MissingOptionalDependency):
            preflight_llm_provider(self._args(), env)

    def test_an_api_key_still_fails_fast(self, block, monkeypatch, tmp_path):  # noqa: F811
        from fluid_build.llm.providers import preflight_llm_provider

        env = self._lean(block, monkeypatch, tmp_path)
        env["GEMINI_API_KEY"] = "AIza-test"  # pragma: allowlist secret
        with pytest.raises(MissingOptionalDependency):
            preflight_llm_provider(self._args(), env)

    def test_a_stated_ollama_host_still_fails_fast(
        self, block, monkeypatch, tmp_path
    ):  # noqa: F811
        from fluid_build.llm.providers import preflight_llm_provider

        env = self._lean(block, monkeypatch, tmp_path)
        env["OLLAMA_HOST"] = "http://127.0.0.1:11500"
        with pytest.raises(MissingOptionalDependency):
            preflight_llm_provider(self._args(), env)

    def test_a_saved_provider_still_fails_fast(self, block, monkeypatch, tmp_path):  # noqa: F811
        from fluid_build.llm.providers import preflight_llm_provider

        env = self._lean(block, monkeypatch, tmp_path, saved={"provider": "openai"})
        with pytest.raises(MissingOptionalDependency):
            preflight_llm_provider(self._args(), env)

    def test_a_saved_agent_passes_the_preflight(self, block, monkeypatch, tmp_path):  # noqa: F811
        # ``fluid ai setup`` saved a coding agent, which needs no litellm: the run
        # uses it, as the selection ladder does.
        from fluid_build.llm.providers import _provider_name_from, preflight_llm_provider

        env = self._lean(block, monkeypatch, tmp_path, saved={"provider": "claude-code"})
        preflight_llm_provider(self._args(), env)
        assert _provider_name_from(self._args(), env) == "claude-code"

    def test_a_saved_agent_with_a_stated_endpoint_still_fails_fast(
        self, block, monkeypatch, tmp_path
    ):  # noqa: F811
        from fluid_build.llm.providers import preflight_llm_provider

        env = self._lean(block, monkeypatch, tmp_path, saved={"provider": "claude-code"})
        with pytest.raises(MissingOptionalDependency):
            preflight_llm_provider(self._args(llm_endpoint="http://onprem.internal/v1"), env)

    def test_a_tty_still_fails_fast(self, block, monkeypatch, tmp_path):  # noqa: F811
        from fluid_build.llm.providers import preflight_llm_provider

        env = self._lean(block, monkeypatch, tmp_path, tty=True)
        with pytest.raises(MissingOptionalDependency):
            preflight_llm_provider(self._args(), env)

    def test_the_agent_loop_never_auto_picks_an_agent(
        self, block, monkeypatch, tmp_path
    ):  # noqa: F811
        from fluid_build.llm.providers import _provider_name_from, preflight_llm_provider

        env = self._lean(block, monkeypatch, tmp_path)
        with pytest.raises(MissingOptionalDependency) as info:
            preflight_llm_provider(self._args(agent_loop=True), env)
        assert "--template starter" in str(info.value)
        assert _provider_name_from(self._args(agent_loop=True), env) != "claude-code"

    def test_the_agent_loop_env_route_never_auto_picks_an_agent(
        self, block, monkeypatch, tmp_path
    ):  # noqa: F811
        from fluid_build.llm.providers import preflight_llm_provider

        env = {**self._lean(block, monkeypatch, tmp_path), "FLUID_COPILOT_AGENT_LOOP": "1"}
        with pytest.raises(MissingOptionalDependency) as info:
            preflight_llm_provider(self._args(), env)
        assert "--template starter" in str(info.value)


def test_agent_search_uses_only_the_given_environment(monkeypatch, tmp_path):
    """An environment without PATH finds no agent, even if the process PATH has one."""
    import stat

    from fluid_build.llm.provider_resolve import _first_coding_agent_on_path

    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "claude"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bindir))
    assert _first_coding_agent_on_path({}) is None
    assert _first_coding_agent_on_path({"PATH": str(bindir)}) == "claude-code"


class TestDoctorStatus:
    def _row(self, monkeypatch):
        from fluid_build.cli.doctor import _check_fluid_features

        _ok, checks = _check_fluid_features()
        return next(c for c in checks if c["check"] == "Forge AI Copilot")

    def test_nothing_configured_says_not_configured(self, block, monkeypatch):  # noqa: F811
        block("litellm")
        for var in (
            "FLUID_LLM_PROVIDER",
            "FLUID_FORGE_AGENT",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "OLLAMA_HOST",
        ):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr("fluid_build._ai_config_shared.load_ai_config", lambda *a, **k: None)
        # Readiness walks the selection ladder: empty its keyring, Ollama and
        # agent rungs at their inputs.
        monkeypatch.setattr("fluid_build.llm.providers._get_api_key_from_keyring", lambda n: None)
        monkeypatch.setattr("fluid_build.llm.providers.detect_ollama_available", lambda env: False)
        monkeypatch.setattr(
            "fluid_build.llm.provider_resolve._first_coding_agent_on_path", lambda env: None
        )
        row = self._row(monkeypatch)
        assert "Not configured" in row["status"]
        assert "fluid ai setup" in row["details"]

    def test_configured_but_unable_to_run_says_not_ready(self, block, monkeypatch):  # noqa: F811
        block("litellm")
        monkeypatch.setenv("FLUID_LLM_PROVIDER", "openai")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real-0000000000000000")
        row = self._row(monkeypatch)
        assert "Not ready" in row["status"]
        assert "pip install 'data-product-forge[litellm]'" in row["details"]
