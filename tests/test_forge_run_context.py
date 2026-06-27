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

"""Structural pins for the ForgeRunContext decomposition of run_ai_copilot_mode.

The interview / domain-enrichment / project-creation "tangled cores" were
extracted from ``run_ai_copilot_mode`` into named helpers, with the three
long-lived dicts folded into a typed :class:`ForgeRunContext` carrier (Fowler's
*Introduce Parameter Object*). Holding ``context`` as an *attribute* is what lets
a phase helper rebind it (``rc.context = …``) and have the orchestrator see the
new value — the thing that defeated a naive Extract Method. Behaviour is pinned
by the 304-test characterization net; these tests pin the new *structure*.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fluid_build.cli import forge_modes as fm

_LOG = logging.getLogger("test.forge.run_context")


def _arg(a, key, default=None):
    return getattr(a, key, default)


def test_forge_run_context_wraps_dicts_by_reference():
    ctx, perf, opts = {"a": 1}, {"b": 2}, {"c": 3}
    rc = fm.ForgeRunContext(
        args=None,
        logger=_LOG,
        console=None,
        copilot=None,
        is_non_interactive=False,
        run_start=0.0,
        active_run_id=None,
        context=ctx,
        perf_stats=perf,
        copilot_options=opts,
    )
    # Wrapping the existing locals must be by-reference (behaviour-preserving).
    assert rc.context is ctx
    assert rc.perf_stats is perf
    assert rc.copilot_options is opts


def test_forge_run_context_default_dicts_are_independent():
    rc1 = fm.ForgeRunContext(
        args=None,
        logger=_LOG,
        console=None,
        copilot=None,
        is_non_interactive=False,
        run_start=0.0,
        active_run_id=None,
    )
    rc2 = fm.ForgeRunContext(
        args=None,
        logger=_LOG,
        console=None,
        copilot=None,
        is_non_interactive=False,
        run_start=0.0,
        active_run_id=None,
    )
    # default_factory: each instance gets its own dicts (no shared-default bug).
    assert rc1.context == {} and rc2.context == {}
    assert rc1.context is not rc2.context


def test_the_three_cores_are_module_level_callables():
    # Extracted phases must be module-level so they're independently testable
    # and the existing patch seams (run_adaptive_copilot_interview etc.) survive.
    for name in (
        "_forge_interview_core",
        "_forge_domain_enrichment_core",
        "_forge_project_creation_core",
    ):
        assert callable(getattr(fm, name)), name


def test_enrichment_core_rebinds_rc_context_across_the_boundary():
    # THE invariant the carrier exists for: a phase helper rebinds rc.context and
    # the orchestrator sees the new object. detect/enrich are imported inside the
    # helper from forge_domain_enrichment, so patch them there.
    new_ctx = {"domain_expertise": "loaded", "marker": "ENRICHED"}
    rc = fm.ForgeRunContext(
        args=SimpleNamespace(domain=None),
        logger=_LOG,
        console=None,
        copilot=None,
        is_non_interactive=True,
        run_start=0.0,
        active_run_id=None,
        context={"project_goal": "x"},
        perf_stats={},
        copilot_options={},
    )
    with (
        patch(
            "fluid_build.cli.forge_domain_enrichment.detect_domain",
            return_value="finance",
        ),
        patch(
            "fluid_build.cli.forge_domain_enrichment.enrich_context_with_domain",
            return_value=new_ctx,
        ),
    ):
        rc_code = fm._forge_domain_enrichment_core(rc, get_cli_arg_fn=_arg)
    assert rc_code is None
    assert rc.context is new_ctx  # rebind crossed the helper boundary


def test_project_creation_core_returns_1_when_creation_fails():
    rc = fm.ForgeRunContext(
        args=SimpleNamespace(),
        logger=_LOG,
        console=None,
        copilot=object(),
        is_non_interactive=True,
        run_start=0.0,
        active_run_id=None,
        context={},
        perf_stats={},
        copilot_options={},
    )
    # No scaffold, no agent-loop -> minimal path; make it fail -> rc 1.
    with patch.object(fm, "_create_project_minimal", return_value=False):
        rc_code = fm._forge_project_creation_core(
            rc, Path("unused"), None, get_cli_arg_fn=lambda a, k, d=None: d
        )
    assert rc_code == 1
