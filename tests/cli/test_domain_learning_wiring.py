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

"""Wiring pins: ``forge_modes`` records + surfaces domain-keyword learning.

The domain-enrichment phase of ``run_ai_copilot_mode`` must
(1) **record** each run's detected domain into ``ai_config.json`` and
(2) **surface** a template nudge for a frequently-built domain on the next
run — driving the ``_ai_setup_storage`` frecency helpers end-to-end.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from fluid_build.cli import forge_modes as fm

_LOG = logging.getLogger("test.forge.domain_learning")


def _arg(a, key, default=None):
    return getattr(a, key, default)


class _Console:
    """Minimal capturing console (matches the ``.print`` surface used)."""

    def __init__(self):
        self.lines: list[str] = []

    def print(self, *args, **kwargs):
        self.lines.append(" ".join(str(a) for a in args))

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture
def iso_config(tmp_path, monkeypatch):
    config_file = tmp_path / "ai_config.json"
    monkeypatch.setattr("fluid_build.cli.ai_setup._CONFIG_FILE", config_file)
    monkeypatch.setattr("fluid_build.cli.ai_setup._CONFIG_DIR", tmp_path)
    return config_file


def _rc(context):
    return fm.ForgeRunContext(
        args=SimpleNamespace(domain=None),
        logger=_LOG,
        console=_Console(),
        copilot=None,
        is_non_interactive=False,
        run_start=0.0,
        active_run_id=None,
        context=dict(context),
    )


FINANCE_CTX = {
    "project_goal": "banking fraud detection",
    "description": "payment risk and regulatory compliance for a fintech",
}

# Identity stub so enrichment doesn't need the real agent-spec machinery.
# ``_forge_domain_enrichment_core`` imports this name from its source module at
# call time, so patch there (late binding flows through).
_IDENTITY_ENRICH = patch(
    "fluid_build.cli.forge_domain_enrichment.enrich_context_with_domain",
    side_effect=lambda c, d: c,
)


def test_enrichment_core_records_detected_domain(iso_config):
    """A forge run with finance-keyword context accumulates finance history."""
    from fluid_build.cli._ai_setup_storage import get_domain_history

    rc = _rc(FINANCE_CTX)
    with _IDENTITY_ENRICH:
        assert fm._forge_domain_enrichment_core(rc, get_cli_arg_fn=_arg) is None
    assert get_domain_history()["finance"]["count"] == 1


def test_repeated_domain_run_surfaces_suggestion(iso_config):
    """After finance crosses the threshold, a neutral next run nudges its template."""
    from fluid_build.cli._ai_setup_storage import record_domain_detection

    # Pre-seed prior runs: finance built 3x already.
    for _ in range(3):
        record_domain_detection("finance")

    # This run detects no domain (neutral) → the frequent finance domain is nudged.
    rc = _rc({"project_goal": "generic widgets tracker"})
    fm._forge_domain_enrichment_core(rc, get_cli_arg_fn=_arg)

    assert "finance" in rc.console.text
    assert "--domain finance" in rc.console.text


def test_active_domain_not_nagged(iso_config):
    """When this run already loads finance, we don't nag about the same pack."""
    from fluid_build.cli._ai_setup_storage import record_domain_detection

    for _ in range(3):
        record_domain_detection("finance")

    # This run itself detects finance → the suggestion excludes it.
    rc = _rc(FINANCE_CTX)
    with _IDENTITY_ENRICH:
        fm._forge_domain_enrichment_core(rc, get_cli_arg_fn=_arg)

    assert "Next time try" not in rc.console.text


def test_record_is_best_effort_never_raises(iso_config, monkeypatch):
    """A storage failure inside personalization must not abort the forge run."""
    monkeypatch.setattr(
        "fluid_build.cli._ai_setup_storage.record_domain_detection",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )
    rc = _rc(FINANCE_CTX)
    with _IDENTITY_ENRICH:
        # Must not raise despite the storage error.
        assert fm._forge_domain_enrichment_core(rc, get_cli_arg_fn=_arg) is None
