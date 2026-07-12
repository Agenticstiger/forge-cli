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

"""Tests for ``fluid forge --prompt-profile <name>``.

A prompt profile swaps the whole set of default-guidance YAML files under
``agent_specs/_defaults/`` for a named directory under
``agent_specs/prompt_profiles/<name>/`` — a single-name, single-swap overlay.

These tests cover:

* the loader/validation surface in ``forge_copilot_prompts`` (discovery,
  activation, unknown-name errors, path-traversal rejection, overlay
  fallback);
* the prompt swap (a profile's prose reaches ``build_system_prompt``);
* the **byte-for-byte baseline guard** — with NO profile active the composed
  system prompt is identical to the checked-in baseline (the critical
  invariant: adding this feature must not change default behaviour);
* the provenance stamp (``metadata.provenance.prompt_profile``) on both the
  blank/guided write path and the AI copilot write path;
* the CLI wiring (flag, ``FLUID_PROMPT_PROFILE`` env fallback, and a clear
  non-silent error on an unknown profile).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from fluid_build.cli import forge_copilot_prompts as P
from fluid_build.cli.forge_contract_factory import stamp_prompt_profile
from fluid_build.cli.forge_copilot_runtime import (
    build_system_prompt,
    clear_system_prompt_cache,
)
from fluid_build.schema_manager import FluidSchemaManager

_REPO_ROOT = Path(__file__).parent.parent
_BASELINE = _REPO_ROOT / "tests" / "data" / "forge_system_prompt_baseline.txt"


def _canonical_matrix() -> dict:
    """Same matrix the baseline snapshot in test_prompt_default_guidance uses."""
    return {
        "providers": ["local", "gcp", "aws", "snowflake"],
        "templates": {
            "starter": {},
            "analytics": {},
            "etl_pipeline": {},
            "ml_pipeline": {},
            "streaming": {},
        },
        "build_engines": ["sql", "python", "dbt", "spark", "custom"],
    }


@pytest.fixture(autouse=True)
def _reset_profile_state():
    """Keep the process-wide profile state from leaking across tests."""
    P.set_prompt_profile(None)
    clear_system_prompt_cache()
    yield
    P.set_prompt_profile(None)
    clear_system_prompt_cache()


# ---------------------------------------------------------------------------
# Loader / validation surface
# ---------------------------------------------------------------------------


class TestProfileLoader:
    def test_bundled_profiles_are_discoverable(self):
        profiles = P.available_prompt_profiles()
        assert "eu-gdpr-strict" in profiles
        assert "ai-lab-permissive" in profiles

    def test_bundled_profile_files_load_cleanly(self):
        # Each bundled profile ships valid YAML with a ``system_prompt`` string
        # under the SAME file names as _defaults/.
        for name in ("eu-gdpr-strict", "ai-lab-permissive"):
            pdir = P._PROFILES_DIR / name
            yamls = sorted(pdir.glob("*.yaml"))
            assert yamls, f"profile {name} ships no yaml files"
            for path in yamls:
                assert path.name in {
                    f.name for f in (P._DEFAULTS_DIR).glob("*.yaml")
                }, f"{path.name} is not a _defaults/ file name"
                raw = yaml.safe_load(path.read_text(encoding="utf-8"))
                assert isinstance(raw, dict)
                assert isinstance(raw.get("system_prompt"), str)
                assert raw["system_prompt"].strip()

    def test_set_get_clear_active_profile(self):
        assert P.get_active_prompt_profile() is None
        assert P.set_prompt_profile("eu-gdpr-strict") == "eu-gdpr-strict"
        assert P.get_active_prompt_profile() == "eu-gdpr-strict"
        assert P.set_prompt_profile(None) is None
        assert P.get_active_prompt_profile() is None
        # Empty string clears too.
        P.set_prompt_profile("eu-gdpr-strict")
        assert P.set_prompt_profile("") is None
        assert P.get_active_prompt_profile() is None

    def test_unknown_profile_raises_and_lists_available(self):
        # Must be a hard error — never a silent fall-back to defaults.
        with pytest.raises(P.PromptProfileError) as exc:
            P.set_prompt_profile("does-not-exist")
        msg = str(exc.value)
        assert "does-not-exist" in msg
        assert "eu-gdpr-strict" in msg  # available profiles listed
        # State stayed clean (no partial activation).
        assert P.get_active_prompt_profile() is None

    @pytest.mark.parametrize(
        "bad",
        [
            "../../etc",
            "..",
            "../ai-lab-permissive",
            "foo/bar",
            "/etc/passwd",
            "a/../b",
            ".hidden",
            "_defaults",  # underscore-prefixed is not a selectable profile
        ],
    )
    def test_traversal_and_unsafe_names_rejected(self, bad):
        with pytest.raises(P.PromptProfileError):
            P.set_prompt_profile(bad)
        assert P.get_active_prompt_profile() is None

    def test_symlink_escape_rejected(self, tmp_path, monkeypatch):
        # A profile dir that is a symlink pointing OUTSIDE the profiles root
        # must be rejected by the resolved-path containment check even though
        # the name itself is a valid slug.
        profiles_root = tmp_path / "prompt_profiles"
        profiles_root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "sovereignty.yaml").write_text("system_prompt: |\n  ESCAPED\n", encoding="utf-8")
        link = profiles_root / "evil"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform")
        monkeypatch.setattr(P, "_PROFILES_DIR", profiles_root)
        with pytest.raises(P.PromptProfileError):
            P.set_prompt_profile("evil")

    def test_overlay_falls_back_to_defaults_for_omitted_files(self, tmp_path, monkeypatch):
        # A profile that only ships sovereignty.yaml keeps every OTHER default
        # guidance block untouched.
        profiles_root = tmp_path / "prompt_profiles"
        (profiles_root / "partial").mkdir(parents=True)
        (profiles_root / "partial" / "sovereignty.yaml").write_text(
            "system_prompt: |\n  PARTIAL_SENTINEL\n", encoding="utf-8"
        )
        monkeypatch.setattr(P, "_PROFILES_DIR", profiles_root)
        P.set_prompt_profile("partial")
        g = P._active_guidance()
        assert "PARTIAL_SENTINEL" in g["sovereignty"]
        # Untouched blocks are the exact default text.
        assert g["agent_policy"] == P._DEFAULT_GUIDANCE["agent_policy"]
        assert g["upstream_sql"] == P._DEFAULT_GUIDANCE["upstream_sql"]
        assert g["technique_mandate"] == P._DEFAULT_GUIDANCE["technique_mandate"]


# ---------------------------------------------------------------------------
# Prompt swap + the byte-for-byte baseline guard (live test #1)
# ---------------------------------------------------------------------------


class TestPromptSwap:
    def test_sentinel_appears_only_when_profile_active(self, tmp_path, monkeypatch):
        sentinel = "SENTINEL_PROFILE_MARKER_XYZZY"
        profiles_root = tmp_path / "prompt_profiles"
        (profiles_root / "x").mkdir(parents=True)
        (profiles_root / "x" / "sovereignty.yaml").write_text(
            f"system_prompt: |\n  {sentinel}\n", encoding="utf-8"
        )
        monkeypatch.setattr(P, "_PROFILES_DIR", profiles_root)

        # No profile: sentinel absent.
        P.set_prompt_profile(None)
        clear_system_prompt_cache()
        baseline = build_system_prompt(_canonical_matrix())
        assert sentinel not in baseline

        # Profile active: sentinel present.
        P.set_prompt_profile("x")
        clear_system_prompt_cache()
        swapped = build_system_prompt(_canonical_matrix())
        assert sentinel in swapped
        assert swapped != baseline

    def test_no_profile_output_matches_checked_in_baseline_byte_for_byte(self):
        # THE GUARDRAIL: with no profile active the composed system prompt is
        # identical to the checked-in baseline. The baseline was captured at an
        # earlier bundled schema version, so we normalise ONLY the fluidVersion
        # token (a pre-existing drift, unrelated to this feature) and then
        # require an exact match — proving this feature added zero bytes to the
        # default path.
        assert _BASELINE.exists(), f"baseline missing: {_BASELINE}"
        baseline_text = _BASELINE.read_text(encoding="utf-8")
        m = re.search(r"FLUID (\d+\.\d+\.\d+) contract", baseline_text)
        assert m, "could not locate the fluidVersion token in the baseline"
        baseline_ver = m.group(1)
        current_ver = FluidSchemaManager.latest_bundled_version()

        P.set_prompt_profile(None)
        clear_system_prompt_cache()
        actual = build_system_prompt(_canonical_matrix())

        normalized = actual.replace(current_ver, baseline_ver)
        assert normalized == baseline_text, (
            "no-profile system prompt drifted from the baseline beyond the "
            "known fluidVersion normalisation — the prompt-profile feature "
            "must not change default output."
        )

    def test_activate_then_clear_is_byte_identical(self):
        # Round-trip identity: activating a real bundled profile and clearing
        # it returns the exact same default prompt.
        P.set_prompt_profile(None)
        clear_system_prompt_cache()
        before = build_system_prompt(_canonical_matrix())

        P.set_prompt_profile("eu-gdpr-strict")
        clear_system_prompt_cache()
        _ = build_system_prompt(_canonical_matrix())

        P.set_prompt_profile(None)
        clear_system_prompt_cache()
        after = build_system_prompt(_canonical_matrix())
        assert after == before

    def test_bundled_gdpr_profile_swaps_sovereignty_block(self):
        P.set_prompt_profile(None)
        clear_system_prompt_cache()
        default = build_system_prompt(_canonical_matrix())
        assert "SOVEREIGNTY BLOCK (optional" in default

        P.set_prompt_profile("eu-gdpr-strict")
        clear_system_prompt_cache()
        gdpr = build_system_prompt(_canonical_matrix())
        assert "SOVEREIGNTY BLOCK (REQUIRED under the EU/GDPR-strict" in gdpr
        # The default upstream_sql block (not overridden) is still present.
        assert "UPSTREAM TRANSFORMATION SQL" in gdpr.upper()

    def test_cache_key_distinguishes_profiles(self):
        # The runtime prompt cache must not hand back a default-guidance prompt
        # after a profile is activated.
        clear_system_prompt_cache()
        P.set_prompt_profile(None)
        default = build_system_prompt(_canonical_matrix())
        P.set_prompt_profile("ai-lab-permissive")
        # NOTE: deliberately do NOT clear the cache here — the profile must be
        # folded into the cache key so this returns the swapped prompt.
        permissive = build_system_prompt(_canonical_matrix())
        assert permissive != default
        assert "AI-lab-permissive" in permissive


# ---------------------------------------------------------------------------
# Provenance stamp — unit
# ---------------------------------------------------------------------------


class TestProvenanceStamp:
    def test_noop_without_active_profile(self):
        P.set_prompt_profile(None)
        contract = {"metadata": {"owner": {"team": "x"}}}
        stamp_prompt_profile(contract)
        assert "provenance" not in contract["metadata"]

    def test_stamps_active_profile(self):
        P.set_prompt_profile("eu-gdpr-strict")
        contract = {"metadata": {"owner": {"team": "x"}}}
        stamp_prompt_profile(contract)
        assert contract["metadata"]["provenance"]["prompt_profile"] == "eu-gdpr-strict"

    def test_creates_metadata_and_provenance_when_missing(self):
        P.set_prompt_profile("ai-lab-permissive")
        contract: dict = {}
        stamp_prompt_profile(contract)
        assert contract["metadata"]["provenance"]["prompt_profile"] == "ai-lab-permissive"

    def test_preserves_existing_provenance_keys(self):
        P.set_prompt_profile("eu-gdpr-strict")
        contract = {"metadata": {"provenance": {"generated_by": "fluid forge"}}}
        stamp_prompt_profile(contract)
        prov = contract["metadata"]["provenance"]
        assert prov["generated_by"] == "fluid forge"
        assert prov["prompt_profile"] == "eu-gdpr-strict"


# ---------------------------------------------------------------------------
# Provenance stamp — end-to-end write paths (live test #2)
# ---------------------------------------------------------------------------


class TestProvenanceEndToEnd:
    def test_write_contract_stamps_profile(self, tmp_path):
        from fluid_build.cli.forge_contract_factory import (
            build_minimal_contract,
            write_contract,
        )

        P.set_prompt_profile("eu-gdpr-strict")
        path = tmp_path / "contract.fluid.yaml"
        write_contract(build_minimal_contract(), path)
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data["metadata"]["provenance"]["prompt_profile"] == "eu-gdpr-strict"

    def test_write_contract_no_stamp_without_profile(self, tmp_path):
        from fluid_build.cli.forge_contract_factory import (
            build_minimal_contract,
            write_contract,
        )

        P.set_prompt_profile(None)
        path = tmp_path / "contract.fluid.yaml"
        write_contract(build_minimal_contract(), path)
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert "prompt_profile" not in data["metadata"].get("provenance", {})

    def test_blank_cli_end_to_end_stamps_provenance(self, tmp_path, monkeypatch):
        # Faithful, fully-offline end-to-end: drive ``fluid forge --blank
        # --prompt-profile eu-gdpr-strict`` through the real CLI entry point
        # and read the emitted contract from disk.
        import logging
        import types

        monkeypatch.setenv("FLUID_FORGE_NO_PICKER", "1")
        monkeypatch.setenv("FLUID_FORGE_NO_WELCOME", "1")
        from fluid_build.cli import forge as forge_cli

        args = types.SimpleNamespace(
            forge_subcommand=None,
            help=False,
            blank=True,
            agent=True,  # skip the interactive CI-scaffold prompt
            prompt_profile="eu-gdpr-strict",
            target_dir=str(tmp_path),
            dry_run=False,
            non_interactive=True,
        )
        rc = forge_cli._run_main(args, logging.getLogger("test.forge"))
        assert rc == 0
        data = yaml.safe_load((tmp_path / "contract.fluid.yaml").read_text(encoding="utf-8"))
        assert data["metadata"]["provenance"]["prompt_profile"] == "eu-gdpr-strict"

    def test_env_fallback_activates_profile_end_to_end(self, tmp_path, monkeypatch):
        import logging
        import types

        monkeypatch.setenv("FLUID_FORGE_NO_PICKER", "1")
        monkeypatch.setenv("FLUID_FORGE_NO_WELCOME", "1")
        monkeypatch.setenv("FLUID_PROMPT_PROFILE", "ai-lab-permissive")
        from fluid_build.cli import forge as forge_cli

        args = types.SimpleNamespace(
            forge_subcommand=None,
            help=False,
            blank=True,
            agent=True,
            prompt_profile=None,  # no CLI flag → env fallback wins
            target_dir=str(tmp_path),
            dry_run=False,
            non_interactive=True,
        )
        rc = forge_cli._run_main(args, logging.getLogger("test.forge"))
        assert rc == 0
        data = yaml.safe_load((tmp_path / "contract.fluid.yaml").read_text(encoding="utf-8"))
        assert data["metadata"]["provenance"]["prompt_profile"] == "ai-lab-permissive"

    def test_cli_unknown_profile_errors_without_writing(self, tmp_path, monkeypatch):
        # Live test #3: an unknown profile is a clear, non-zero error — the CLI
        # does NOT silently fall back and does NOT write a contract.
        import logging
        import types

        monkeypatch.setenv("FLUID_FORGE_NO_PICKER", "1")
        monkeypatch.setenv("FLUID_FORGE_NO_WELCOME", "1")
        from fluid_build.cli import forge as forge_cli

        args = types.SimpleNamespace(
            forge_subcommand=None,
            help=False,
            blank=True,
            agent=True,
            prompt_profile="totally-made-up",
            target_dir=str(tmp_path),
            dry_run=False,
            non_interactive=True,
        )
        rc = forge_cli._run_main(args, logging.getLogger("test.forge"))
        assert rc != 0
        assert not (tmp_path / "contract.fluid.yaml").exists()

    def test_ai_path_create_forge_config_stamps_contract(self):
        # The AI copilot write path stamps the generated contract before it
        # becomes the engine's write payload (``copilot_generated_contract``).
        from fluid_build.cli.forge import CopilotAgent
        from fluid_build.cli.forge_copilot_runtime import (
            CopilotGenerationResult,
            DiscoveryReport,
        )

        P.set_prompt_profile("eu-gdpr-strict")
        agent = CopilotAgent()
        gr = CopilotGenerationResult(
            suggestions={"recommended_template": "starter", "recommended_provider": "local"},
            contract={"name": "p", "fluidVersion": "0.7.4", "metadata": {"owner": {"team": "x"}}},
            readme_markdown="# p\n",
            additional_files={},
            discovery_report=DiscoveryReport(workspace_roots=["/tmp"]),
            attempt_reports=[],
        )
        cfg = agent._create_forge_config(Path("/tmp/p"), {"project_goal": "p"}, gr.suggestions, gr)
        contract = cfg["copilot_generated_contract"]
        assert contract["metadata"]["provenance"]["prompt_profile"] == "eu-gdpr-strict"

    def test_ai_path_serializes_stamp_to_contract_yaml(self):
        # The real engine ContractGenerator serialises the stamped contract to
        # ``contract.fluid.yaml`` with the stamp intact.
        from fluid_build.forge.core.interfaces import GenerationContext
        from fluid_build.forge.generators.contract_generator import ContractGenerator

        P.set_prompt_profile("eu-gdpr-strict")
        contract = {"name": "p", "fluidVersion": "0.7.4", "metadata": {"owner": {"team": "x"}}}
        stamp_prompt_profile(contract)
        ctx = GenerationContext(
            project_config={"copilot_generated_contract": contract},
            target_dir=Path("/tmp/p"),
            template_metadata={},
            provider_config={},
            user_selections={},
            forge_version="0.0.0",
            creation_time="1970-01-01T00:00:00Z",
        )
        out = ContractGenerator().generate(ctx)
        data = yaml.safe_load(out["contract.fluid.yaml"])
        assert data["metadata"]["provenance"]["prompt_profile"] == "eu-gdpr-strict"
