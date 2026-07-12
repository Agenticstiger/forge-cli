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

"""Per-tenant / per-domain prompt override (Trello 69e53ea2).

Two no-fork override mechanisms compose UNDER the ``--prompt-profile`` system
added in #359:

* **User-home shadow** — a tenant drops ``<stem>.yaml`` into
  ``<user-home>/agent_specs/_defaults/`` (``$FLUID_USER_HOME`` → ``~/.fluid``)
  to override the matching bundled ``_defaults/`` guidance block.
* **Per-domain fragments** — ``agent_specs/<domain>.yaml`` carries an optional
  ``system_prompt_fragments`` map applied only while that domain is active
  (via ``enrich_context_with_domain``).

Documented precedence, lowest → highest::

    bundled _defaults  <  user-home shadow  <  domain fragments  <  --prompt-profile

These tests are the live-test plan for the card:

1. User-dir shadow wins over the bundled default; removing it reverts to the
   byte-for-byte baseline.
2. A per-domain fragment applies only when that domain is active.
3. With ``--prompt-profile`` ALSO active, the documented precedence holds.
4. Security — the shadow dir can't be used to read files outside it (no
   symlink / traversal escape), YAML is ``safe_load`` only, and an unsafe
   domain name never activates fragments.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import fluid_build.cli.forge_agent_specs as A
from fluid_build.cli import forge_copilot_prompts as P
from fluid_build.cli.forge_copilot_runtime import (
    build_system_prompt,
    clear_system_prompt_cache,
)
from fluid_build.cli.forge_domain_enrichment import enrich_context_with_domain
from fluid_build.schema_manager import FluidSchemaManager

_REPO_ROOT = Path(__file__).parent.parent
_BASELINE = _REPO_ROOT / "tests" / "data" / "forge_system_prompt_baseline.txt"

# The default sovereignty block prose (present unless overridden).
_DEFAULT_SOVEREIGNTY_MARKER = "SOVEREIGNTY BLOCK (optional"


def _canonical_matrix() -> dict:
    """Same matrix the #359 baseline snapshot uses."""
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


def _write_shadow(home: Path, stem: str, sentinel: str) -> Path:
    """Drop ``<home>/agent_specs/_defaults/<stem>.yaml`` with a sentinel."""
    ddir = home / "agent_specs" / "_defaults"
    ddir.mkdir(parents=True, exist_ok=True)
    path = ddir / f"{stem}.yaml"
    path.write_text(f"system_prompt: |\n  {sentinel}\n", encoding="utf-8")
    return path


_DOMAIN_SPEC_TEMPLATE = """\
name: {name}
domain: {name}
description: Synthetic domain for prompt-fragment tests.
questions:
  - key: goal
    question: What is your goal?
    type: text
suggestion_defaults:
  recommended_template: starter
  recommended_provider: local
{fragments}
"""


def _write_domain_spec(agents_dir: Path, name: str, sovereignty_sentinel: str) -> Path:
    agents_dir.mkdir(parents=True, exist_ok=True)
    fragments = "system_prompt_fragments:\n" "  sovereignty: |\n" f"    {sovereignty_sentinel}\n"
    path = agents_dir / f"{name}.yaml"
    path.write_text(_DOMAIN_SPEC_TEMPLATE.format(name=name, fragments=fragments), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _reset_override_state(monkeypatch, tmp_path):
    """Isolate all process-wide override state + point the user-home at a clean
    empty tmp dir so a real ``~/.fluid/agent_specs/_defaults`` on the dev box
    can never contaminate a test.
    """
    monkeypatch.setenv("FLUID_USER_HOME", str(tmp_path / "clean_home"))
    P.set_prompt_profile(None)
    P.set_domain_prompt_fragments(None, None)
    clear_system_prompt_cache()
    yield
    P.set_prompt_profile(None)
    P.set_domain_prompt_fragments(None, None)
    clear_system_prompt_cache()


# ---------------------------------------------------------------------------
# The shadow-dir discovery helper
# ---------------------------------------------------------------------------


class TestShadowDirHelper:
    def test_honours_fluid_user_home(self, tmp_path, monkeypatch):
        home = tmp_path / "tenant_home"
        (home / "agent_specs" / "_defaults").mkdir(parents=True)
        monkeypatch.setenv("FLUID_USER_HOME", str(home))
        dirs = A.user_defaults_shadow_dirs()
        assert dirs == [(home / "agent_specs" / "_defaults").resolve()]

    def test_empty_when_dir_absent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FLUID_USER_HOME", str(tmp_path / "no_such_home"))
        assert A.user_defaults_shadow_dirs() == []


# ---------------------------------------------------------------------------
# Live test #1 — user-dir shadow wins, then reverts byte-for-byte
# ---------------------------------------------------------------------------


class TestUserHomeShadow:
    def test_shadow_overrides_bundled_default(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        _write_shadow(home, "sovereignty", "SHADOW_SOVEREIGNTY_SENTINEL")
        monkeypatch.setenv("FLUID_USER_HOME", str(home))
        clear_system_prompt_cache()

        prompt = build_system_prompt(_canonical_matrix())
        assert "SHADOW_SOVEREIGNTY_SENTINEL" in prompt
        # The bundled sovereignty prose was REPLACED, not appended.
        assert _DEFAULT_SOVEREIGNTY_MARKER not in prompt
        # An un-shadowed block (upstream_sql) is untouched.
        assert "UPSTREAM TRANSFORMATION SQL" in prompt.upper()

    def test_remove_shadow_reverts_to_byte_identical_baseline(self, tmp_path, monkeypatch):
        # Baseline: empty home (no shadow).
        empty = tmp_path / "empty_home"
        monkeypatch.setenv("FLUID_USER_HOME", str(empty))
        clear_system_prompt_cache()
        baseline = build_system_prompt(_canonical_matrix())

        # Add a shadow → output changes.
        shadow_home = tmp_path / "shadow_home"
        _write_shadow(shadow_home, "sovereignty", "TEMP_SENTINEL")
        monkeypatch.setenv("FLUID_USER_HOME", str(shadow_home))
        clear_system_prompt_cache()
        swapped = build_system_prompt(_canonical_matrix())
        assert swapped != baseline
        assert "TEMP_SENTINEL" in swapped

        # Remove the shadow (point home back at the empty dir) → byte-identical.
        monkeypatch.setenv("FLUID_USER_HOME", str(empty))
        clear_system_prompt_cache()
        reverted = build_system_prompt(_canonical_matrix())
        assert reverted == baseline

    def test_no_shadow_matches_checked_in_baseline_byte_for_byte(self):
        # THE GUARDRAIL (hermetic): with the shadow machinery active but no
        # shadow present (empty FLUID_USER_HOME from the autouse fixture), the
        # composed prompt equals the checked-in baseline — proving this feature
        # adds zero bytes to the default path. Only the fluidVersion token is
        # normalised (a pre-existing #359 drift, unrelated to this feature).
        assert _BASELINE.exists(), f"baseline missing: {_BASELINE}"
        baseline_text = _BASELINE.read_text(encoding="utf-8")
        m = re.search(r"FLUID (\d+\.\d+\.\d+) contract", baseline_text)
        assert m, "could not locate the fluidVersion token in the baseline"
        baseline_ver = m.group(1)
        current_ver = FluidSchemaManager.latest_bundled_version()

        clear_system_prompt_cache()
        actual = build_system_prompt(_canonical_matrix())
        normalized = actual.replace(current_ver, baseline_ver)
        assert normalized == baseline_text, (
            "no-shadow system prompt drifted from the baseline beyond the known "
            "fluidVersion normalisation — the per-tenant override feature must "
            "not change default output."
        )

    def test_active_guidance_returns_default_object_on_pure_default_path(self):
        # Object-identity fast path: no shadow, no domain, no profile ⇒ the
        # exact bundled map is returned (guarantees byte-identical baseline).
        assert P._active_guidance() is P._DEFAULT_GUIDANCE


# ---------------------------------------------------------------------------
# Live test #2 — per-domain fragment applies only when the domain is active
# ---------------------------------------------------------------------------


class TestDomainFragments:
    def test_fragment_applies_only_when_domain_active(self, tmp_path, monkeypatch):
        agents = tmp_path / "agents"
        _write_domain_spec(agents, "spacex", "DOMAIN_SOVEREIGNTY_SENTINEL")
        monkeypatch.setattr(A, "_user_agent_dirs", lambda: [agents])

        # Not active yet.
        clear_system_prompt_cache()
        assert "DOMAIN_SOVEREIGNTY_SENTINEL" not in build_system_prompt(_canonical_matrix())

        # Enrich with the domain → fragment activates.
        ctx = enrich_context_with_domain({"project_goal": "rockets"}, "spacex")
        assert P.get_active_domain() == "spacex"
        assert ctx["domain_expertise"]["domain"] == "spacex"
        clear_system_prompt_cache()
        active = build_system_prompt(_canonical_matrix())
        assert "DOMAIN_SOVEREIGNTY_SENTINEL" in active
        assert _DEFAULT_SOVEREIGNTY_MARKER not in active

    def test_enriching_fragmentless_domain_clears_prior_overlay(self, tmp_path, monkeypatch):
        agents = tmp_path / "agents"
        _write_domain_spec(agents, "spacex", "DOMAIN_SOVEREIGNTY_SENTINEL")
        monkeypatch.setattr(A, "_user_agent_dirs", lambda: [agents])

        enrich_context_with_domain({"project_goal": "rockets"}, "spacex")
        assert P.get_active_domain() == "spacex"

        # A builtin domain with NO system_prompt_fragments (finance) must reset
        # the overlay on entry — no leak of the previous domain's fragment.
        enrich_context_with_domain({"project_goal": "loans"}, "finance")
        assert P.get_active_domain() is None
        clear_system_prompt_cache()
        assert "DOMAIN_SOVEREIGNTY_SENTINEL" not in build_system_prompt(_canonical_matrix())


# ---------------------------------------------------------------------------
# Live test #3 — precedence when multiple layers stack
# ---------------------------------------------------------------------------


class TestPrecedence:
    def test_bundled_lt_shadow_lt_domain_lt_profile(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        _write_shadow(home, "sovereignty", "SHADOW_SENTINEL")
        monkeypatch.setenv("FLUID_USER_HOME", str(home))

        # 1. bundled only (empty home) → default marker present.
        monkeypatch.setenv("FLUID_USER_HOME", str(tmp_path / "empty"))
        assert _DEFAULT_SOVEREIGNTY_MARKER in P._active_guidance()["sovereignty"]

        # 2. shadow only → shadow wins over bundled.
        monkeypatch.setenv("FLUID_USER_HOME", str(home))
        assert P._active_guidance()["sovereignty"].strip() == "SHADOW_SENTINEL"

        # 3. shadow + domain → domain wins over shadow.
        P.set_domain_prompt_fragments("d", {"sovereignty": "DOMAIN_SENTINEL"})
        assert P._active_guidance()["sovereignty"] == "DOMAIN_SENTINEL"

        # 4. shadow + domain + profile → profile wins over everything.
        P.set_prompt_profile("eu-gdpr-strict")
        sov = P._active_guidance()["sovereignty"]
        assert "SOVEREIGNTY BLOCK (REQUIRED under the EU/GDPR-strict" in sov
        assert "DOMAIN_SENTINEL" not in sov
        assert "SHADOW_SENTINEL" not in sov

        # An un-overridden block still falls through to the bundled default.
        assert P._active_guidance()["upstream_sql"] == P._DEFAULT_GUIDANCE["upstream_sql"]

    def test_cache_key_tracks_shadow_content_without_manual_clear(self, tmp_path, monkeypatch):
        # The runtime prompt cache must invalidate when the shadow CONTENT
        # changes, even in the same home, without an explicit cache clear.
        home = tmp_path / "home"
        _write_shadow(home, "sovereignty", "SENTINEL_A")
        monkeypatch.setenv("FLUID_USER_HOME", str(home))
        clear_system_prompt_cache()
        first = build_system_prompt(_canonical_matrix())
        assert "SENTINEL_A" in first

        # Overwrite the same file; do NOT clear the cache.
        _write_shadow(home, "sovereignty", "SENTINEL_B")
        second = build_system_prompt(_canonical_matrix())
        assert "SENTINEL_B" in second
        assert "SENTINEL_A" not in second


# ---------------------------------------------------------------------------
# Live test #4 — security
# ---------------------------------------------------------------------------


class TestSecurity:
    def test_symlink_in_shadow_dir_cannot_escape(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        ddir = home / "agent_specs" / "_defaults"
        ddir.mkdir(parents=True)
        outside = tmp_path / "outside" / "secret.yaml"
        outside.parent.mkdir(parents=True)
        outside.write_text("system_prompt: |\n  ESCAPED_SECRET\n", encoding="utf-8")
        link = ddir / "sovereignty.yaml"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform")

        monkeypatch.setenv("FLUID_USER_HOME", str(home))
        clear_system_prompt_cache()
        prompt = build_system_prompt(_canonical_matrix())
        # The symlinked-out file is ignored; the bundled default is retained.
        assert "ESCAPED_SECRET" not in prompt
        assert _DEFAULT_SOVEREIGNTY_MARKER in prompt

    def test_shadow_yaml_is_safe_load_only(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        ddir = home / "agent_specs" / "_defaults"
        ddir.mkdir(parents=True)
        # A python-object tag would execute under yaml.load; safe_load raises,
        # so the file is skipped and the default is retained (no code runs).
        (ddir / "sovereignty.yaml").write_text(
            "system_prompt: !!python/object/apply:os.system ['echo pwned']\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("FLUID_USER_HOME", str(home))
        clear_system_prompt_cache()
        prompt = build_system_prompt(_canonical_matrix())  # must not raise
        assert _DEFAULT_SOVEREIGNTY_MARKER in prompt

    def test_unsafe_domain_name_never_activates_fragments(self, tmp_path, monkeypatch):
        monkeypatch.setattr(A, "_user_agent_dirs", lambda: [tmp_path / "agents"])
        # A traversal-style domain name loads no spec and activates nothing.
        enrich_context_with_domain({"project_goal": "x"}, "../evil")
        assert P.get_active_domain() is None

    def test_safe_domain_regex_rejects_traversal(self):
        assert P is not None  # keep import ordering explicit
        from fluid_build.cli.forge_domain_enrichment import _SAFE_DOMAIN_RE

        for bad in ("../evil", "a/b", "..", "/etc/passwd", ".hidden", ""):
            assert not _SAFE_DOMAIN_RE.match(bad)
        for good in ("finance", "spacex", "my-domain", "d1.2_x"):
            assert _SAFE_DOMAIN_RE.match(good)

    def test_setter_filters_non_string_fragments(self):
        # Defence: only string→string entries survive.
        assert P.set_domain_prompt_fragments("d", {"sovereignty": 123}) is None
        assert P.get_active_domain() is None
        assert P.set_domain_prompt_fragments("d", {"sovereignty": "ok", "x": None}) == "d"
        g = P._active_guidance()
        assert g["sovereignty"] == "ok"
        assert "x" not in g  # non-string value dropped
