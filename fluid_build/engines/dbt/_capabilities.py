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

"""Which YAML shapes the target dbt engine actually understands.

dbt v2 rejects three shapes forge emits today, and the corrected shapes are
NOT universally safe on dbt 1.x — so each one needs a floor. The floors below
were measured by generating each shape, running ``dbt parse``/``dbt source
freshness``, and reading ``target/manifest.json`` on every listed version,
because two of the three fail SILENTLY rather than erroring:

    shape                      v2 rejects the old form   new form honoured from
    -------------------------  ------------------------  ----------------------
    model ``access``           dbt1060 UnusedConfigKey    1.8.10 (all tested)
    source freshness /         dbt1060 UnusedConfigKey    1.10.5
      ``loaded_at_field``
    generic-test arguments     dbt1159 DbtYamlValidation  1.10.8

Measurements (dbt-core unless noted), ``dbt source freshness`` behaviour for
the freshness row and ``dbt parse`` for the others:

    1.8.10   freshness: source skipped entirely; arguments: compile error
    1.9.11   freshness: "loaded_at_field must be specified" -> config IGNORED
    1.10.0   freshness: config IGNORED (silent);  arguments: compile error
    1.10.5   freshness: honoured;                 arguments: compile error*
    1.10.8   freshness: honoured;                 arguments: honoured
    1.12.5   all honoured                         (flat args warn: dbt1159 class)
    2.0.4    all honoured                         (old forms are hard errors)
    (dbt-oss 2.0.4 is the Apache-2.0 build of the same v2 engine.)

    * ``arguments:`` is introduced at 1.10.5 but gated behind the
      ``require_generic_test_arguments_property`` behaviour flag, which only
      defaults to true at 1.10.8. Since forge does not control the consumer's
      ``dbt_project.yml`` flags, 1.10.8 is the floor it can rely on.
      See https://docs.getdbt.com/reference/global-configs/behavior-flags/
      require_generic_test_arguments_property

Why this matters: forge declares a floor of ``dbt-core>=1.7`` (pyproject
extras and the generated CI bootstrap), so a consumer really can be below
these versions. Emitting the v2-correct shape unconditionally would silently
stop honouring the contract's freshness promise on anything under 1.10.5 —
it still parses, it just quietly does nothing.

The transformations themselves mirror dbt Labs' own ``dbt-autofix``
(Apache-2.0, https://github.com/dbt-labs/dbt-autofix): ``refactor_test_args``
moves non-reserved test keys under ``arguments``, and
``restructure_yaml_keys_for_node`` moves config-eligible top-level fields
under ``config``. forge does not depend on it — it is a post-hoc fixer that
fetches its schemas from a CDN with no offline fallback, reindents the whole
file and deletes keys forge emits deliberately. Borrowed as a pattern, and
cross-checked against the authoritative v2 spec
(``fs-schema-dbt-yaml-files-v2.0.5.json``): ``ModelProperties`` has no
``access`` while ``ModelConfig`` does; ``TablesConfig`` carries ``freshness``
and ``loaded_at_field``; ``CustomTestInner`` is ``additionalProperties:
false`` over ``{arguments, column_name, config, description, name}``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Tuple

#: ``config:``-scoped source freshness is stored but NOT read by
#: ``dbt source freshness`` below this version.
FRESHNESS_CONFIG_FLOOR: Tuple[int, int, int] = (1, 10, 5)

#: ``arguments:``-nested generic-test inputs are a compile error below this
#: version (1.10.5 introduces them behind a flag that defaults off).
TEST_ARGUMENTS_FLOOR: Tuple[int, int, int] = (1, 10, 8)

_VERSION_RE = re.compile(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?")


def _version_tuple(version: str) -> Optional[Tuple[int, int, int]]:
    """Parse ``1.10.8`` / ``1.10.8b1`` / ``2.0.4`` to a comparable triple.

    Unparseable or empty returns ``None`` so callers fall back to the legacy
    shape rather than guessing.
    """
    if not version:
        return None
    match = _VERSION_RE.match(version.strip().lstrip("v"))
    if match is None:
        return None
    major, minor, patch = match.groups()
    return (int(major), int(minor or 0), int(patch or 0))


@dataclass(frozen=True)
class DbtCapabilities:
    """The YAML shapes this target engine understands.

    ``config_scoped_access`` has no floor in the tested range, so it carries
    no flag — callers always emit ``config: {access: ...}``.
    """

    #: Nest generic-test inputs under ``arguments:`` (dbt >= 1.10.8, or v2).
    nest_test_arguments: bool = False
    #: Put source ``freshness``/``loaded_at_field`` under ``config:``
    #: (dbt >= 1.10.5, or v2).
    config_scoped_source_freshness: bool = False


#: What forge emits when it cannot tell which engine will run the project.
#: Mirrors ``_resolve_tests_key``'s "no dbt binary -> legacy key" precedent:
#: the legacy shapes parse on every dbt 1.x, and a consumer actually running
#: v2 will have a v2 binary on PATH for the detector to find.
LEGACY = DbtCapabilities()

#: Everything the v2 engine requires.
MODERN = DbtCapabilities(nest_test_arguments=True, config_scoped_source_freshness=True)


def resolve_dbt_capabilities(flavor: Optional[str], version: str) -> DbtCapabilities:
    """Map a detected ``(flavor, version)`` to the shapes forge may emit.

    ``flavor`` comes from ``build_runners.dbt.runner._detect_dbt_engine``;
    ``engines/`` may not import that module (import tiering), so the resolved
    pair is passed in by the CLI instead.
    """
    if flavor == "fusion":
        # The v2 engine rejects every legacy shape outright.
        return MODERN
    if flavor != "core":
        return LEGACY
    parsed = _version_tuple(version)
    if parsed is None:
        return LEGACY
    return DbtCapabilities(
        nest_test_arguments=parsed >= TEST_ARGUMENTS_FLOOR,
        config_scoped_source_freshness=parsed >= FRESHNESS_CONFIG_FLOOR,
    )
