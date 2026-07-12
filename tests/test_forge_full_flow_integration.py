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

"""Full-flow Forge integration: config SAVE -> NEW process -> LOAD -> copilot starts.

Trello 69d4c9d6 — the existing AI-setup coverage (``tests/test_ai_setup.py``,
``tests/test_ai_multi_provider.py``) is unit-heavy: it patches
``_CONFIG_FILE`` / keyring in-process and asserts a same-interpreter
round-trip. That leaves the *cross-process boundary* — the property
operators actually depend on — untested: does a config persisted by one
``fluid`` invocation get picked up by the NEXT, genuinely separate,
``python -m fluid_build.cli`` process so the copilot can start?

These tests exercise that real boundary. Each test:

* runs the REAL production save path
  (``fluid_build.cli.ai_setup._save_ai_config`` — the same call the
  interactive ``fluid ai setup`` wizard makes) in one child process, then
* loads it back in a *different* child process — the real
  ``python -m fluid_build.cli ai status`` CLI and/or the copilot readiness
  gate (``check_llm_readiness``) — and asserts on its output / exit code.

Hermeticity (the ``env -u ANTHROPIC_API_KEY -u OPENAI_API_KEY
-u GEMINI_API_KEY`` guarantee, done in code):

* **Isolated ``$HOME``.** ``_CONFIG_DIR = Path.home() / ".fluid"`` is
  resolved at import time, so pointing a child's ``HOME`` at a ``tmp_path``
  gives it a pristine ``~/.fluid``. A leaked read of the developer's real
  config would break :meth:`test_config_is_isolated_between_homes`, so the
  isolation is self-checking.
* **No OS keychain.** ``PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring``
  forces the "no keyring available" backend, which raises on every op and
  never touches the real Keychain/Secret-Service. Persistence therefore
  flows through the opt-in **plaintext-gate** file
  (``FLUID_ALLOW_PLAINTEXT_AI_SECRETS=1`` -> the fake key is written into the
  isolated ``ai_config.json``). This is the ONLY hermetic surface that can
  actually carry a secret across a process boundary: an in-memory keyring
  dies with its process, and the real keychain is not hermetic — so the
  plaintext-gate file is the correct mechanism for a cross-process test.
* **No real key, no network, no LLM call.** Every credential here is an
  obviously-fake token; the readiness gate is a pure config resolver (no
  HTTP), and no forge/LLM turn is ever driven.

CI-safe: marked ``integration`` (the free ``ci.yml::duckdb-integration`` job
selects ``-m "integration and not slow"``), and skips cleanly if the CLI
package cannot be imported. No keyring *backend* is a prerequisite — the
plaintext-gate path is used precisely so a missing/broken keyring is a
no-op, not a skip.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

# The free duckdb-integration CI job selects on ``integration and not slow``.
pytestmark = [pytest.mark.integration]

# Repo root == parent of ``tests/``. Children run with this on ``PYTHONPATH``
# so they import the SAME tree the test lives in (not a stale editable install).
_REPO_ROOT = Path(__file__).resolve().parents[1]

# Skip cleanly on a stripped image where the CLI package can't import at all.
try:  # pragma: no cover - import guard
    import fluid_build.cli.ai_setup  # noqa: F401
except Exception as _exc:  # pragma: no cover - defensive
    pytest.skip(
        f"fluid_build.cli.ai_setup import failed: {_exc}",
        allow_module_level=True,
    )

# Obviously-fake credentials — never a real key. The provider is always
# selected explicitly, so the key's format is irrelevant to resolution.
_FAKE_OPENAI_KEY = "sk-fake-not-a-real-openai-key"
_FAKE_GEMINI_KEY = "AIza-fake-not-a-real-gemini-key"

# Child scripts print a single ``FLUID_PROBE=<json>`` line so the parent can
# parse a stable payload regardless of any Rich/logging noise on the streams.
_PROBE_TAG = "FLUID_PROBE="

# Persist one provider via the REAL production storage function — the same
# call ``fluid ai setup`` makes when the wizard confirms a provider.
_SAVE_SCRIPT = """\
import sys
from fluid_build.cli.ai_setup import _save_ai_config
_provider, _model, _key = sys.argv[1], sys.argv[2], (sys.argv[3] or None)
print("SAVED" if _save_ai_config(_provider, _model, api_key=_key) else "FAILED")
"""

# The copilot/readiness gate — what forge consults to decide it can start.
_READINESS_SCRIPT = """\
import json
from fluid_build.cli.forge_copilot_llm_providers import check_llm_readiness
_r = check_llm_readiness()
print(
    "FLUID_PROBE="
    + json.dumps(
        {
            "ready": _r.ready,
            "provider": _r.provider,
            "model": _r.model,
            "auth": _r.auth_available,
            "error": _r.error,
        }
    )
)
"""

# Read the persisted multi-provider map back (active + full provider list).
_LIST_SCRIPT = """\
import json
from fluid_build.cli.ai_setup import _list_configured_providers, _load_ai_config
_cfg = _load_ai_config() or {}
print(
    "FLUID_PROBE="
    + json.dumps({"providers": _list_configured_providers(), "active": _cfg.get("provider")})
)
"""

# Report exactly what the ACTIVE provider config resolves to on disk.
_LOAD_SCRIPT = """\
import json
from fluid_build.cli.ai_setup import _load_ai_config
print("FLUID_PROBE=" + json.dumps({"config": _load_ai_config()}))
"""

# Flip the active/default provider (like ``gh auth switch``) in its own process.
_SWITCH_SCRIPT = """\
import sys
from fluid_build.cli.ai_setup import _set_active_provider
print("SWITCHED" if _set_active_provider(sys.argv[1]) else "NOOP")
"""


def _hermetic_env(
    home: Path,
    *,
    plaintext: bool = False,
    forced_provider: str | None = None,
) -> dict:
    """Build a minimal, hermetic environment for a fresh ``fluid`` child.

    Constructed from scratch (NOT ``os.environ.copy()``) so no hosted API
    key, provider selector, or ``OLLAMA_HOST`` from the parent shell can
    leak in — this is the in-code equivalent of the card's ``env -u
    ANTHROPIC_API_KEY -u OPENAI_API_KEY -u GEMINI_API_KEY``.

    Notably ``OLLAMA_HOST`` is left UNSET: it is one of the explicit
    provider signals, so setting it (even to a dead port) would make the
    readiness ladder pick ``ollama`` *before* consulting the saved config —
    exactly the resolution we're trying to prove.
    """
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(_REPO_ROOT),
        # Neutralise the real OS keychain — the fail backend raises on every
        # op and never touches it, so the plaintext-gate file is the only
        # persistence surface.
        "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring",
        "PYTHONIOENCODING": "utf-8",
    }
    if plaintext:
        env["FLUID_ALLOW_PLAINTEXT_AI_SECRETS"] = "1"
    if forced_provider:
        env["FLUID_LLM_PROVIDER"] = forced_provider
    return env


def _run_child(argv: list[str], env: dict, *, timeout: int = 120) -> subprocess.CompletedProcess:
    """Run a genuinely separate interpreter and capture its streams."""
    return subprocess.run(
        argv,
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _save_config_process(
    home: Path,
    provider: str,
    model: str,
    *,
    api_key: str | None,
    plaintext: bool,
) -> subprocess.CompletedProcess:
    """Persist one provider's config from a separate WRITER process."""
    env = _hermetic_env(home, plaintext=plaintext)
    return _run_child([sys.executable, "-c", _SAVE_SCRIPT, provider, model, api_key or ""], env)


def _parse_probe(cp: subprocess.CompletedProcess) -> dict:
    """Extract the ``FLUID_PROBE=<json>`` payload a probe child emitted."""
    for line in cp.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(_PROBE_TAG):
            return json.loads(stripped[len(_PROBE_TAG) :])
    raise AssertionError(
        f"child did not emit {_PROBE_TAG!r} (rc={cp.returncode}).\n"
        f"--- stdout ---\n{cp.stdout}\n--- stderr ---\n{cp.stderr}"
    )


def _probe_process(
    home: Path,
    script: str,
    *,
    forced_provider: str | None = None,
) -> tuple[subprocess.CompletedProcess, dict]:
    """Run a probe READER process and return (completed, parsed payload)."""
    env = _hermetic_env(home, forced_provider=forced_provider)
    cp = _run_child([sys.executable, "-c", script], env)
    return cp, _parse_probe(cp)


def _cli_process(
    home: Path,
    *cli_args: str,
    forced_provider: str | None = None,
) -> subprocess.CompletedProcess:
    """Run the REAL ``python -m fluid_build.cli <args>`` as a fresh process."""
    env = _hermetic_env(home, forced_provider=forced_provider)
    return _run_child([sys.executable, "-m", "fluid_build.cli", *cli_args], env)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """An isolated ``$HOME`` — every child gets a pristine ``~/.fluid``."""
    h = tmp_path / "home"
    h.mkdir()
    return h


class TestForgeFullFlowConfigPersistence:
    """SAVE (process A) -> LOAD (process B) across a real process boundary."""

    def test_saved_config_loads_in_a_fresh_cli_process(self, home: Path) -> None:
        """The headline property: a config written by one process is read by
        a genuinely separate ``python -m fluid_build.cli ai status`` process.
        """
        # --- Process A: persist openai + a fake key via the real save path.
        writer = _save_config_process(
            home, "openai", "gpt-4o", api_key=_FAKE_OPENAI_KEY, plaintext=True
        )
        assert writer.returncode == 0, writer.stderr
        assert "SAVED" in writer.stdout, writer.stdout

        # The file really landed on disk under the ISOLATED home, owner-only.
        config_file = home / ".fluid" / "ai_config.json"
        assert config_file.exists(), "writer did not persist ai_config.json"
        assert stat.S_IMODE(config_file.stat().st_mode) == 0o600, oct(config_file.stat().st_mode)

        # --- Process B: the REAL CLI, a different interpreter, clean env.
        status = _cli_process(home, "ai", "status")
        assert status.returncode == 0, status.stderr
        low = status.stdout.lower()
        assert "openai" in low, status.stdout
        assert "ready" in low, status.stdout
        assert "not configured" not in low, status.stdout

    def test_saved_config_opens_the_copilot_readiness_gate(self, home: Path) -> None:
        """The persisted config makes ``check_llm_readiness`` (the gate forge
        consults before starting the copilot) report READY in a fresh process.
        """
        writer = _save_config_process(
            home, "openai", "gpt-4o", api_key=_FAKE_OPENAI_KEY, plaintext=True
        )
        assert "SAVED" in writer.stdout, writer.stderr

        cp, payload = _probe_process(home, _READINESS_SCRIPT)
        assert cp.returncode == 0, cp.stderr
        assert payload["ready"] is True, payload
        assert payload["provider"] == "openai", payload
        assert payload["model"] == "gpt-4o", payload
        assert payload["auth"] is True, payload
        assert payload["error"] is None, payload

    def test_config_is_isolated_between_homes(self, tmp_path: Path) -> None:
        """Self-check on the harness: a config saved under one ``$HOME`` must
        not bleed into a *different* ``$HOME`` — proving every other test reads
        the tmp home, never the developer's real ``~/.fluid``.
        """
        home_a = tmp_path / "a"
        home_a.mkdir()
        home_b = tmp_path / "b"
        home_b.mkdir()

        writer = _save_config_process(
            home_a, "openai", "gpt-4o", api_key=_FAKE_OPENAI_KEY, plaintext=True
        )
        assert "SAVED" in writer.stdout, writer.stderr

        # Home B never saw a save -> nothing to load (ambient-independent:
        # this reads the file only, so a dev-box Ollama can't confound it).
        _, empty = _probe_process(home_b, _LOAD_SCRIPT)
        assert empty["config"] is None, empty

        # Home A still resolves to what it persisted.
        _, filled = _probe_process(home_a, _LOAD_SCRIPT)
        assert filled["config"] is not None, filled
        assert filled["config"]["provider"] == "openai", filled

    def test_unconfigured_process_reports_not_ready(self, home: Path) -> None:
        """An unconfigured, keyless process must NOT start the copilot: the
        readiness gate stays closed and the CLI points the user at setup.

        A provider is forced (``FLUID_LLM_PROVIDER``) so the check short-
        circuits before the last-resort ambient-Ollama probe — the result is
        deterministic on any box, with or without a local Ollama.
        """
        cp, payload = _probe_process(home, _READINESS_SCRIPT, forced_provider="openai")
        assert cp.returncode == 0, cp.stderr
        assert payload["ready"] is False, payload
        assert payload["auth"] is False, payload
        assert payload["provider"] == "openai", payload
        assert payload["error"] and "api key" in payload["error"].lower(), payload

        # The real CLI surfaces the same not-ready state and a setup nudge.
        status = _cli_process(home, "ai", "status", forced_provider="openai")
        assert status.returncode == 0, status.stderr
        low = status.stdout.lower()
        assert "no api key" in low, status.stdout
        assert "fluid ai setup" in low, status.stdout

    def test_multiple_providers_persist_across_processes(self, home: Path) -> None:
        """Two independent WRITER processes each add a provider; a fresh
        READER sees BOTH in the map — the second save never clobbers the
        first, across the process boundary (mirrors the in-process
        ``test_ai_multi_provider`` invariant, proven end-to-end here).
        """
        first = _save_config_process(
            home, "openai", "gpt-4o", api_key=_FAKE_OPENAI_KEY, plaintext=True
        )
        second = _save_config_process(
            home, "gemini", "gemini-2.5-pro", api_key=_FAKE_GEMINI_KEY, plaintext=True
        )
        assert "SAVED" in first.stdout and "SAVED" in second.stdout

        _, listing = _probe_process(home, _LIST_SCRIPT)
        assert set(listing["providers"]) == {"openai", "gemini"}, listing
        # Last write wins the active/default marker.
        assert listing["active"] == "gemini", listing

        # The readiness gate resolves the active provider end-to-end.
        _, payload = _probe_process(home, _READINESS_SCRIPT)
        assert payload["ready"] is True, payload
        assert payload["provider"] == "gemini", payload
        assert payload["model"] == "gemini-2.5-pro", payload

        # The CLI status surface names both saved providers.
        status = _cli_process(home, "ai", "status")
        low = status.stdout.lower()
        assert "openai" in low and "gemini" in low, status.stdout

    def test_active_provider_switch_persists_across_processes(self, home: Path) -> None:
        """Switching the active/default provider in one process is observed by
        the next — the ``active`` marker is durable, not just in-memory.
        """
        _save_config_process(home, "openai", "gpt-4o", api_key=_FAKE_OPENAI_KEY, plaintext=True)
        _save_config_process(
            home, "gemini", "gemini-2.5-pro", api_key=_FAKE_GEMINI_KEY, plaintext=True
        )

        switch = _run_child(
            [sys.executable, "-c", _SWITCH_SCRIPT, "openai"],
            _hermetic_env(home),
        )
        assert "SWITCHED" in switch.stdout, switch.stderr

        _, payload = _probe_process(home, _READINESS_SCRIPT)
        assert payload["ready"] is True, payload
        assert payload["provider"] == "openai", payload
        assert payload["model"] == "gpt-4o", payload

    def test_plaintext_gate_decision_persists_across_processes(self, home: Path) -> None:
        """The security gate travels across the boundary too: a WRITER run
        *without* ``FLUID_ALLOW_PLAINTEXT_AI_SECRETS`` (and with the keychain
        neutralised) persists the provider CHOICE but drops the secret — so a
        fresh READER knows the provider yet the copilot stays not-ready.
        """
        writer = _save_config_process(
            home, "openai", "gpt-4o", api_key=_FAKE_OPENAI_KEY, plaintext=False
        )
        assert "SAVED" in writer.stdout, writer.stderr

        # The key was NOT written to disk (gate closed).
        on_disk = json.loads((home / ".fluid" / "ai_config.json").read_text(encoding="utf-8"))
        assert "api_key" not in on_disk["providers"]["openai"], on_disk

        # A fresh reader: provider choice survived, but no key -> not ready.
        _, payload = _probe_process(home, _READINESS_SCRIPT)
        assert payload["provider"] == "openai", payload
        assert payload["ready"] is False, payload
        assert payload["auth"] is False, payload
        assert payload["error"] and "api key" in payload["error"].lower(), payload
