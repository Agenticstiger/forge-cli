# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid ai models --provider`` takes the LLM provider registry.

The flag wrote ``args.provider``, the slot of the global infrastructure
``--provider``. The global check then read every value as an infrastructure
provider, so ``fluid ai models --provider gemini`` exited 2 with "Unknown
provider 'gemini' — installed providers: aws, databricks, gcp, local,
snowflake". The flag's own ``choices`` were five hard-coded names, behind the
names ``fluid forge --llm-provider`` takes.
"""

from __future__ import annotations

import importlib.metadata as md
import json
import re
from typing import Any, Dict, Mapping

import pytest

from fluid_build.cli import _validate_global_args, build_parser, main
from fluid_build.cli._llm_provider_plugins import (
    BUILTIN_PROVIDER_CHOICES,
    LLM_PROVIDER_GROUP,
    reset_plugin_registry,
)
from fluid_build.cli.ai_setup import show_ai_models
from fluid_build.cli.forge_copilot_llm_providers import LlmProvider


class _PluginProvider(LlmProvider):
    """A third-party provider, as a ``fluid_build.llm_providers`` entry point loads it."""

    name = "acme-llm"
    default_model = "acme-large"

    def default_endpoint(self, model: str, env: Mapping[str, str]) -> str:
        return "https://acme.invalid/v1"

    def build_request(self, config, system_prompt, user_prompt):
        return ({}, {})

    def extract_text(self, response_json: Dict[str, Any]) -> str:
        return ""


class _EntryPoint:
    name = "acme-llm"

    def load(self) -> Any:
        return _PluginProvider


@pytest.fixture(autouse=True)
def _isolate_plugin_registry():
    reset_plugin_registry()
    yield
    reset_plugin_registry()


@pytest.fixture
def acme_plugin(monkeypatch):
    def entry_points(group=None, **_kw):
        return [_EntryPoint()] if group == LLM_PROVIDER_GROUP else []

    monkeypatch.setattr(md, "entry_points", entry_points)


@pytest.fixture(scope="module")
def parser():
    return build_parser()


class _Log:
    def __init__(self):
        self.errors = []

    def log_safe(self, level, message, **_kw):
        if level == "error":
            self.errors.append(message)


def test_every_registry_name_stays_off_the_infrastructure_slot(parser):
    for name in BUILTIN_PROVIDER_CHOICES:
        args = parser.parse_args(["ai", "models", "--provider", name])
        assert args.llm_provider == name
        assert getattr(args, "provider", None) is None
        log = _Log()
        _validate_global_args(args, log, known_providers=["aws", "gcp", "local"])
        assert log.errors == []


def test_an_unknown_name_is_refused_with_exactly_the_registry(parser, capsys):
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["ai", "models", "--provider", "not-a-provider"])
    assert exc.value.code == 2
    # Python 3.12.8+ prints the names bare, older versions quote them.
    listed = re.search(r"choose from (.+)\)", capsys.readouterr().err).group(1)
    assert [name.strip("'\" ") for name in listed.split(",")] == list(BUILTIN_PROVIDER_CHOICES)


def test_the_command_prints_the_plan_it_was_asked_for(capsys):
    assert main(["ai", "models", "--provider", "gemini", "--json"]) == 0
    assert list(json.loads(capsys.readouterr().out)) == ["gemini"]


@pytest.mark.parametrize("name", BUILTIN_PROVIDER_CHOICES)
def test_every_registry_name_has_a_plan(name, capsys):
    show_ai_models(None, provider_filter=name, as_json=True)
    plans = json.loads(capsys.readouterr().out)
    shown = "anthropic" if name == "claude" else name
    assert list(plans) == [shown]
    assert plans[shown]["primary_model"]


def test_a_plugin_provider_is_accepted_and_shown(acme_plugin, capsys):
    args = build_parser().parse_args(["ai", "models", "--provider", "acme-llm"])
    assert args.llm_provider == "acme-llm"
    show_ai_models(None, provider_filter="acme-llm", as_json=True)
    plans = json.loads(capsys.readouterr().out)
    assert plans["acme-llm"]["primary_model"] == "acme-large"


def test_a_name_outside_the_registry_shows_nothing(capsys):
    show_ai_models(None, provider_filter="not-a-provider", as_json=True)
    assert json.loads(capsys.readouterr().out) == {}
