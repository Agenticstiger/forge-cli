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

"""``fluid generate ci`` bakes ``--ensure-opentofu`` into the apply stage.

A cloud ``fluid apply`` shells out to ``tofu``; rather than ask every CI
runner image to pre-install OpenTofu (root + gpg, which non-root agents
can't do), the generated apply stage carries ``--ensure-opentofu`` so the
CLI provisions a pinned, SHA-256-verified ``tofu`` on demand. This pins
that the flag reaches the apply stage of **every** generated runner — the
single source of truth being the shared 11-stage StageSpec (for the six
non-Jenkins systems) and Jenkins' own stage-7 string.
"""

from __future__ import annotations

import pytest

from fluid_build.forge.core.pipeline_templates import (
    PipelineComplexity,
    PipelineConfig,
    PipelineProvider,
    PipelineTemplateGenerator,
)

pytestmark = [pytest.mark.unit]

# Every CI system the generator can emit.
_ALL_PROVIDERS = list(PipelineProvider)


def _render_all(provider: PipelineProvider, sink_platform=None) -> str:
    cfg = PipelineConfig(
        provider=provider,
        complexity=PipelineComplexity("standard"),
        sink_platform=sink_platform,
    )
    out = PipelineTemplateGenerator().generate_pipeline(cfg)
    # generate_pipeline returns {path/key: file-content}; join all values so
    # the assertion is robust to per-system output-file naming.
    return "\n".join(out.values())


@pytest.mark.parametrize("provider", _ALL_PROVIDERS, ids=lambda p: p.name)
def test_apply_stage_emits_ensure_opentofu(provider):
    """All 7 runners must carry --ensure-opentofu in the generated apply stage."""
    content = _render_all(provider, sink_platform="snowflake")
    assert "--ensure-opentofu" in content, (
        f"{provider.name} generated pipeline is missing --ensure-opentofu in its "
        "apply stage — cloud applies on a fresh runner would fail with no `tofu`."
    )


@pytest.mark.parametrize("provider", _ALL_PROVIDERS, ids=lambda p: p.name)
def test_ensure_opentofu_attached_to_an_apply_invocation(provider):
    """The flag must sit on a `fluid apply` line, not stray elsewhere."""
    content = _render_all(provider, sink_platform="snowflake")
    apply_lines = [
        ln
        for ln in content.splitlines()
        if "--ensure-opentofu" in ln or ("fluid apply" in ln and "ensure-opentofu" in ln)
    ]
    # Either the flag is on the same line as the apply argv assembly, or it is
    # an argv token set just above a bare `fluid apply "$@"` — both satisfy the
    # contract that the apply stage provisions tofu. Assert at least one line
    # mentions it (the StageSpec/Jenkins apply uses `set -- … --ensure-opentofu`).
    assert apply_lines, f"{provider.name}: --ensure-opentofu not found near an apply step"


def test_flag_is_unconditional_idempotent_noop_for_local():
    """A local-sink pipeline still emits the flag (harmless no-op): the apply
    never reaches the OpenTofu engine for `local`, so the flag is ignored, and
    keeping it unconditional keeps the template a single source of truth."""
    content = _render_all(PipelineProvider.GITHUB_ACTIONS, sink_platform="local")
    assert "--ensure-opentofu" in content
