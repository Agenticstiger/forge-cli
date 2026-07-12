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

"""``fluid forge --offline`` — the local, no-network guided authoring path.

Covers the four things that matter for the Trello card (#69d4c9ca):

1. argparse accepts ``--offline`` (flag parses, defaults to off).
2. ``--offline`` routes to the guided mode handler and *only* that —
   never the AI copilot, blank, or template handlers, and never the
   mode picker (proving no network-touching path runs).
3. ``FLUID_FORGE_OFFLINE=1`` is an equivalent env twin (mirrors
   ``cargo --offline`` / ``CARGO_NET_OFFLINE``).
4. True-offline end-to-end: with every socket call booby-trapped to
   raise, ``fluid forge --offline --non-interactive`` still exits 0,
   writes ``contract.fluid.yaml``, and the result passes
   ``fluid validate --offline`` — with zero connection attempts.
"""

from __future__ import annotations

import argparse
import logging
import socket
import sys
from pathlib import Path
from unittest import mock

import pytest

from fluid_build.cli import forge as forge_mod
from fluid_build.cli import forge_modes as forge_modes_mod

LOGGER = logging.getLogger("test.forge.offline")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_forge_args(*extra: str) -> argparse.Namespace:
    """Build the real ``fluid forge`` parser and parse ``forge <extra...>``.

    Using the production parser (rather than a hand-rolled Namespace)
    guarantees every default matches what argparse actually produces,
    so the routing tests exercise the real attribute surface.
    """
    parser = argparse.ArgumentParser(prog="fluid")
    subparsers = parser.add_subparsers(dest="command")
    forge_mod.register(subparsers)
    return parser.parse_args(["forge", *extra])


def _parse_validate_args(*extra: str) -> argparse.Namespace:
    from fluid_build.cli import validate as validate_mod

    parser = argparse.ArgumentParser(prog="fluid")
    subparsers = parser.add_subparsers(dest="command")
    validate_mod.register(subparsers)
    return parser.parse_args(["validate", *extra])


@pytest.fixture(autouse=True)
def _clear_offline_env(monkeypatch):
    """Never inherit a real FLUID_FORGE_OFFLINE from the dev shell."""
    monkeypatch.delenv("FLUID_FORGE_OFFLINE", raising=False)
    # Keep the picker/preview off so nothing interactive sneaks in even
    # on the paths that would reach them.
    monkeypatch.setenv("FLUID_FORGE_NO_PICKER", "1")
    monkeypatch.setenv("FLUID_FORGE_NO_PREVIEW", "1")
    yield


def _drive_forge(args: argparse.Namespace):
    """Run ``forge.run`` with every mode handler stubbed to a counter.

    Returns ``(called, rc)`` where ``called`` records exactly which
    handlers fired — the assertion is "one handler ran", so a
    regression where offline also reaches the picker/AI shows up.
    """
    called = {"guided": 0, "blank": 0, "ai": 0, "template": 0, "picker": 0}

    def _stub_guided(_args, _logger):
        called["guided"] += 1
        return 0

    def _stub_blank(_args, _logger):
        called["blank"] += 1
        return 0

    def _stub_ai(_args, _logger):
        called["ai"] += 1
        return 0

    def _stub_template(_args, _logger, **_kw):
        called["template"] += 1
        return 0

    def _stub_pick_mode(_args, **_kw):
        called["picker"] += 1
        return "ai"

    with (
        mock.patch.object(forge_mod, "run_guided_mode", _stub_guided),
        mock.patch.object(forge_mod, "_run_blank_mode", _stub_blank),
        mock.patch.object(forge_mod, "run_ai_copilot_mode", _stub_ai),
        mock.patch.object(forge_modes_mod, "run_template_mode", _stub_template),
        mock.patch("fluid_build.cli._forge_mode_picker.pick_mode", _stub_pick_mode),
        mock.patch.object(forge_mod, "_write_forge_receipt", lambda **_kw: None),
        mock.patch.object(forge_mod, "_print_forge_next_steps", lambda *_a, **_kw: None),
    ):
        rc = forge_mod.run(args, LOGGER)
    return called, rc


# ---------------------------------------------------------------------------
# 1. Flag parsing
# ---------------------------------------------------------------------------


def test_offline_flag_parses_true():
    args = _parse_forge_args("--offline")
    assert args.offline is True


def test_offline_flag_defaults_false():
    args = _parse_forge_args()
    assert args.offline is False


# ---------------------------------------------------------------------------
# 2. _forge_offline_requested — flag + env twin
# ---------------------------------------------------------------------------


def test_offline_requested_via_flag():
    args = _parse_forge_args("--offline")
    assert forge_mod._forge_offline_requested(args) is True


def test_offline_requested_via_env(monkeypatch):
    args = _parse_forge_args()  # flag off
    monkeypatch.setenv("FLUID_FORGE_OFFLINE", "1")
    assert forge_mod._forge_offline_requested(args) is True


@pytest.mark.parametrize("falsey", ["0", "false", "no", "off", ""])
def test_offline_env_falsey_spellings_are_off(monkeypatch, falsey):
    args = _parse_forge_args()
    monkeypatch.setenv("FLUID_FORGE_OFFLINE", falsey)
    assert forge_mod._forge_offline_requested(args) is False


def test_offline_env_truthy_word(monkeypatch):
    args = _parse_forge_args()
    monkeypatch.setenv("FLUID_FORGE_OFFLINE", "yes")
    assert forge_mod._forge_offline_requested(args) is True


# ---------------------------------------------------------------------------
# 3. Routing — offline goes to guided, nothing else
# ---------------------------------------------------------------------------


def test_offline_routes_to_guided_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    args = _parse_forge_args("--offline", "-d", str(tmp_path / "prod"))
    called, rc = _drive_forge(args)
    assert rc == 0
    assert called["guided"] == 1, f"expected guided=1, got {called}"
    assert called["ai"] == 0, "offline must never reach the AI copilot"
    assert called["blank"] == 0
    assert called["template"] == 0
    assert called["picker"] == 0, "offline must never consult the mode picker"


def test_offline_env_routes_to_guided(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FLUID_FORGE_OFFLINE", "1")
    args = _parse_forge_args("-d", str(tmp_path / "prod"))  # no --offline flag
    called, rc = _drive_forge(args)
    assert rc == 0
    assert called["guided"] == 1, f"expected guided=1, got {called}"
    assert called["ai"] == 0
    assert called["picker"] == 0


def test_offline_blank_precedence_goes_to_blank(tmp_path, monkeypatch):
    """``--offline --blank`` honours blank (also offline, more specific)."""
    monkeypatch.chdir(tmp_path)
    args = _parse_forge_args("--offline", "--blank", "-d", str(tmp_path / "prod"))
    called, rc = _drive_forge(args)
    assert rc == 0
    assert called["blank"] == 1, f"expected blank=1, got {called}"
    assert called["guided"] == 0
    assert called["ai"] == 0


# ---------------------------------------------------------------------------
# 4. Non-interactive default derivation
# ---------------------------------------------------------------------------


def test_non_interactive_defaults_bare():
    args = _parse_forge_args()
    pid, domain, provider = forge_modes_mod._guided_non_interactive_defaults(args)
    assert pid == "my-data-product"
    assert domain == "analytics"
    assert provider == "local"


def test_non_interactive_defaults_from_flags(tmp_path):
    args = _parse_forge_args(
        "-d", str(tmp_path / "orders_pipeline"), "--provider", "snowflake", "--domain", "finance"
    )
    pid, domain, provider = forge_modes_mod._guided_non_interactive_defaults(args)
    assert pid == "orders_pipeline"
    assert domain == "finance"
    assert provider == "snowflake"


def test_interactive_guided_still_requires_tty(monkeypatch):
    """The TTY guard stays for the *interactive* path (regression guard)."""
    args = _parse_forge_args()  # non_interactive is False
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    rc = forge_modes_mod.run_guided_mode(
        args, LOGGER, get_target_directory_fn=lambda a, n: Path(n), console_factory=None
    )
    assert rc == 1


# ---------------------------------------------------------------------------
# 5. True-offline end-to-end — sockets booby-trapped
# ---------------------------------------------------------------------------


class _NetworkAttempted(AssertionError):
    """Raised if anything tries to open a socket during offline forge."""


@pytest.fixture
def block_all_network(monkeypatch):
    """Booby-trap every outbound socket path so any network use fails loudly."""

    def _boom(*_a, **_k):
        raise _NetworkAttempted("network access attempted during offline mode")

    monkeypatch.setattr(socket.socket, "connect", _boom)
    monkeypatch.setattr(socket.socket, "connect_ex", _boom)
    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    yield


def test_true_offline_end_to_end(tmp_path, monkeypatch, block_all_network):
    """`fluid forge --offline --non-interactive` writes a valid contract with no network."""
    monkeypatch.chdir(tmp_path)
    # Non-interactive so no TTY is needed and no prompt blocks the run.
    target = tmp_path / "customer_events"
    args = _parse_forge_args(
        "--offline", "--non-interactive", "-d", str(target), "--provider", "local"
    )

    rc = forge_mod.run(args, LOGGER)
    assert rc == 0, "offline non-interactive forge should exit 0"

    contract_path = target / "contract.fluid.yaml"
    assert contract_path.exists(), "contract.fluid.yaml must be written offline"

    # The generated contract must pass the strict, offline schema validator
    # — while sockets are STILL booby-trapped, proving validation is local too.
    v_args = _parse_validate_args(str(contract_path), "--offline")
    v_rc = forge_validate_run(v_args)
    assert v_rc == 0, "generated contract must pass `fluid validate --offline`"


def forge_validate_run(v_args: argparse.Namespace) -> int:
    from fluid_build.cli import validate as validate_mod

    return validate_mod.run(v_args, LOGGER)


def test_true_offline_dry_run_no_network(tmp_path, monkeypatch, block_all_network):
    """`--offline --dry-run` short-circuits before writing, still no network."""
    monkeypatch.chdir(tmp_path)
    args = _parse_forge_args(
        "--offline", "--non-interactive", "--dry-run", "-d", str(tmp_path / "p")
    )
    rc = forge_mod.run(args, LOGGER)
    assert rc == 0
    assert not (tmp_path / "p" / "contract.fluid.yaml").exists()
