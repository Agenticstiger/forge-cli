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

"""``packages.yml`` emitter — pin the dbt packages the generated project references.

Generated dbt projects reference package-namespaced generic tests
(``dbt_expectations.expect_column_values_to_be_between``,
``dbt_utils.expression_is_true``, ``dbt_utils.recency`` — see
:mod:`._test_mapping`) and, for some templates, ``dbt_utils.*`` macros in
model SQL. Previously no ``packages.yml`` was ever emitted, so any generated
project with range/freshness tests failed its own ``--dbt-validate``
``dbt parse`` gate until the user hand-authored one. Prior art:
``datacontract-cli``'s dbt exporter emits the same namespaced tests and leaves
package installation to the user — we deliberately go further and emit the
pins so ``dbt deps && dbt parse`` succeeds out of the box.

Design decisions (each verified against the dbt docs / hubs at build time):

* **Needed-only emission.** The pins are derived by scanning the *emitted*
  file contents for the ``dbt_utils.`` / ``dbt_expectations.`` namespace
  tokens. A plain not_null/unique/accepted_values project gets no
  ``packages.yml`` at all. Over-matching (e.g. a description mentioning
  ``dbt_utils.``) errs toward including a package — harmless, ``dbt deps``
  simply installs it; under-matching would be the actual bug.
* **Pin shape.** dbt's own recommendation
  (https://docs.getdbt.com/docs/build/packages) is to range-pin to the latest
  patch of a known-good minor (``[">=X.Y.0", "<X.(Y+1).0"]``); we follow it
  verbatim. Current pins: ``dbt-labs/dbt_utils`` 1.4.x
  (require-dbt-version ``>=1.3.0,<3.0.0``) and ``metaplane/dbt_expectations``
  0.10.x (require-dbt-version ``>=1.7.0,<3.0.0``). dbt_expectations
  maintenance moved from calogica to Metaplane — the hub's ``latest`` lives
  under the ``metaplane`` org.
* **dependencies.yml interaction.** dbt forbids ``packages.yml`` and
  ``dependencies.yml`` coexisting in one project. ``--mesh-hub`` emits a
  ``dependencies.yml`` (``schema_yml._mesh_only_output``), so when that file
  is present the package pins are folded into its ``packages:`` key instead
  (dependencies.yml carries both ``projects:`` and ``packages:`` keys; legal
  because our specs contain no Jinja).
* **Merge-not-overwrite.** Users commonly hand-maintain ``packages.yml``. An
  existing file *without* the ``# managed-by: fluid`` sentinel is never
  touched — we skip emission and log a warning listing the required pins so
  the user can reconcile. A sentinel-carrying file is fluid-owned and is
  regenerated. (Leave-untouched-and-tell is the safer, simpler behaviour vs.
  merging entries into a file we don't own; it mirrors the
  ``fluid generate dbt-tests`` refusal semantics for ``schema.yml``.)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

# Must stay byte-identical to ``exporters.dbt_tests.MANAGED_BY_SENTINEL``.
# Not imported from there: ``exporters.dbt_tests`` imports
# ``engines.dbt._test_mapping`` (executing this package's ``__init__``), so a
# module-scope import back into ``exporters`` would be circular. Equality is
# pinned in ``tests/engines/test_dbt_packages_yml.py``.
MANAGED_BY_SENTINEL = "# managed-by: fluid"

# Namespace token → hub pin. Versions follow dbt's recommended
# latest-patch-of-a-minor range (receipts in the module docstring).
PACKAGE_PINS: Dict[str, Dict[str, Any]] = {
    "dbt_utils": {
        "package": "dbt-labs/dbt_utils",
        "version": [">=1.4.0", "<1.5.0"],
    },
    "dbt_expectations": {
        "package": "metaplane/dbt_expectations",
        "version": [">=0.10.0", "<0.11.0"],
    },
}


def required_packages(files: Mapping[str, str]) -> list[str]:
    """Return the package names any emitted file content references.

    Scans every generated file (schema/sources YAML *and* model SQL — user
    SQL may call ``dbt_utils.generate_surrogate_key()`` etc.) for the
    ``<package>.`` namespace token. Deterministic order (PACKAGE_PINS order).
    """
    needed: list[str] = []
    for pkg in PACKAGE_PINS:
        token = f"{pkg}."
        if any(token in content for content in files.values()):
            needed.append(pkg)
    return needed


def render_packages_yml(needed: Sequence[str]) -> str:
    """Render a sentinel-headed ``packages.yml`` for the needed packages."""
    import yaml

    doc = {"packages": [PACKAGE_PINS[pkg] for pkg in needed]}
    body = yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)
    return (
        f"{MANAGED_BY_SENTINEL}\n"
        "# Generated from fluid contract — dbt package pins for the\n"
        "# package-namespaced tests/macros in this project. Run `dbt deps`.\n"
        f"{body}"
    )


def merge_into_dependencies_yml(content: str, needed: Sequence[str]) -> str:
    """Fold package pins into an emitted ``dependencies.yml``.

    dbt forbids ``packages.yml`` and ``dependencies.yml`` coexisting;
    ``dependencies.yml`` accepts both ``projects:`` (mesh hubs) and
    ``packages:`` keys, so the pins ride along there. Existing ``packages:``
    entries win on collision (by hub package name).
    """
    import yaml

    doc = yaml.safe_load(content) or {}
    if not isinstance(doc, dict):  # defensive — never clobber unknown shapes
        return content
    packages = [p for p in doc.get("packages") or [] if isinstance(p, Mapping)]
    present = {p.get("package") for p in packages}
    for pkg in needed:
        pin = PACKAGE_PINS[pkg]
        if pin["package"] not in present:
            packages.append(dict(pin))
    doc["packages"] = packages
    # Preserve any leading comment header the original emitter wrote.
    header_lines = []
    for line in content.splitlines():
        if line.startswith("#") or not line.strip():
            header_lines.append(line)
        else:
            break
    header = ("\n".join(header_lines) + "\n") if header_lines else ""
    return header + yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)


def inject_package_pins(
    files: Dict[str, str],
    *,
    output_dir: Optional[Path] = None,
) -> Dict[str, str]:
    """Add the needed dbt package pins to a generated-files dict, in place.

    * No package-namespaced reference anywhere → no-op (a plain
      not_null-only project gets no ``packages.yml``).
    * ``dependencies.yml`` present (``--mesh-hub``) → pins merge into it.
    * An existing on-disk ``packages.yml`` without the fluid sentinel →
      left untouched; a warning lists the pins the user must reconcile.
    * Otherwise → ``packages.yml`` is (re-)emitted with the sentinel.
    """
    needed = required_packages(files)
    if not needed:
        return files

    if "dependencies.yml" in files:
        files["dependencies.yml"] = merge_into_dependencies_yml(files["dependencies.yml"], needed)
        return files

    if output_dir is not None:
        existing = Path(output_dir) / "packages.yml"
        if existing.exists():
            try:
                text = existing.read_text(encoding="utf-8")
            except OSError:
                text = ""
            if MANAGED_BY_SENTINEL not in text:
                logger.warning(
                    "packages_yml_left_untouched: %s is user-managed (missing "
                    "'%s'); the generated project needs these dbt packages — "
                    "add them to your packages.yml and run `dbt deps`: %s",
                    existing,
                    MANAGED_BY_SENTINEL,
                    ", ".join(
                        f"{PACKAGE_PINS[p]['package']} {PACKAGE_PINS[p]['version']}" for p in needed
                    ),
                )
                return files

    files["packages.yml"] = render_packages_yml(needed)
    return files


__all__ = [
    "MANAGED_BY_SENTINEL",
    "PACKAGE_PINS",
    "required_packages",
    "render_packages_yml",
    "merge_into_dependencies_yml",
    "inject_package_pins",
]
