# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Which kind of catalog each Spark catalog the scanned code uses is.

``glue_catalog.raw.orders`` names a Spark catalog: an alias the job's session was configured with
(``spark.sql.catalog.glue_catalog = org.apache.iceberg.spark.SparkCatalog``). FLUID's
``binding.location.catalog`` is a *kind* (``glue``, ``rest``, ``hive``, ``nessie``, ...), and an
alias is no kind: copying one into the binding writes a contract ``fluid validate`` refuses. The
kind is known only when the code says so:

* ``spark.sql.catalog.<alias>.type`` is a kind FLUID knows (``glue``, ``rest``, ``hive``,
  ``nessie``, ``hadoop``, ``jdbc``), or
* ``spark.sql.catalog.<alias>.catalog-impl`` is Iceberg's ``GlueCatalog``, ``RESTCatalog``,
  ``HiveCatalog``, ``NessieCatalog`` or ``HadoopCatalog``,

set as a constant in the code that is scanned: ``SparkSession.builder.config(key, value)``,
``spark.conf.set(key, value)``, a dict entry (``SparkSubmitOperator(conf={...})``), or
``--conf key=value`` in any string (a ``spark-submit`` command, ``sparkSubmitParameters``, an
argument list). Nothing is run and nothing is read from outside the scanned files. An alias the
code sets to two kinds is no kind.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ._fold import Folder
from ._pysource import Module

#: Iceberg's catalog classes, by the name a ``catalog-impl`` ends in, and the kind each one is.
IMPL_KINDS = {
    "GlueCatalog": "glue",
    "RESTCatalog": "rest",
    "HiveCatalog": "hive",
    "NessieCatalog": "nessie",
    "HadoopCatalog": "hadoop",
}
_NEEDLE = "spark.sql.catalog"
_KEY = re.compile(r"spark\.sql\.catalog\.([A-Za-z0-9_-]{1,64})\.(type|catalog-impl)", re.I)
#: ``key=value`` inside a string: a ``--conf`` of a command line or of ``sparkSubmitParameters``.
_PAIR = re.compile(
    r"(spark\.sql\.catalog\.[A-Za-z0-9_-]{1,64}\.(?:type|catalog-impl))\s*=\s*([^\s,;'\"]{1,200})",
    re.I,
)
#: Values folded and settings kept from one file: a file that sets more is not about catalogs.
MAX_FOLDS = 2_000
MAX_SETTINGS = 500


def setting(key: Any, value: Any) -> Optional[Tuple[str, str]]:
    """``(alias, kind)`` when ``key = value`` says which kind of catalog the Spark catalog
    ``alias`` is, else ``None``. The alias is lower case, as a table name is read."""
    if not isinstance(key, str) or not isinstance(value, str):
        return None
    found = _KEY.fullmatch(key.strip())
    if found is None:
        return None
    alias, which = found.group(1).lower(), found.group(2).lower()
    text = value.strip().strip("'\"")
    if which == "type":
        candidate = text
    else:
        candidate = IMPL_KINDS.get(text.rsplit(".", 1)[-1], "")
    if not candidate:
        return None
    from fluid_build.providers._iceberg_catalog import (
        FAMILY_UNKNOWN,
        canonical_catalog_kind,
        catalog_kind_info,
    )

    kind = canonical_catalog_kind(candidate)
    if not kind or catalog_kind_info(kind).family == FAMILY_UNKNOWN:
        return None
    return alias, kind


def _setter(node: ast.AST) -> Optional[ast.Call]:
    """``node`` when it is ``<x>.config(key, value)`` or ``<x>.set(key, value)`` (a session
    builder, ``spark.conf``, a ``SparkConf``), else ``None``."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"config", "set"}
        and len(node.args) >= 2
    ):
        return node
    return None


def _mentions(node: ast.AST) -> Optional[bool]:
    """``True`` when ``node`` is a string constant that names a Spark catalog setting, ``False``
    when it is one that does not, ``None`` when it is not a constant (it has to be folded)."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and _NEEDLE in node.value
    return None


def module_settings(module: Module, folder: Folder) -> List[Tuple[str, str]]:
    """The ``(alias, kind)`` settings ``module`` fixes (see the module's docstring), in the order
    they are first written. Run after the module's own reads, so what it folds never takes the
    work budget they need."""
    tree = module.tree
    if not any(_mentions(node) for node in ast.walk(tree)):
        return []
    found: Dict[Tuple[str, str], None] = {}
    folds = 0

    def take(key: Any, value: Any) -> None:
        pair = setting(key, value)
        if pair is not None:
            found.setdefault(pair)

    def pairs_in(text: Any) -> None:
        if isinstance(text, str) and _NEEDLE in text:
            for match in _PAIR.finditer(text):
                take(match.group(1), match.group(2))

    for node in ast.walk(tree):
        if folds >= MAX_FOLDS or len(found) >= MAX_SETTINGS:
            break
        call = _setter(node)
        if call is not None:
            if _mentions(call.args[0]) is not False:  # a constant that names none is skipped
                folds += 2
                take(folder.string(call.args[0]), folder.string(call.args[1]))
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if key is not None and _mentions(key) is not False:
                    folds += 2
                    take(folder.string(key), folder.string(value))
        elif isinstance(node, ast.Constant):
            pairs_in(node.value)
        elif isinstance(node, ast.JoinedStr):
            folds += 1
            pairs_in(folder.string(node))
    return list(found)


@dataclass
class CatalogKinds:
    """What the scanned code says each Spark catalog is: the kinds it was set to, by alias."""

    kinds: Dict[str, Set[str]] = field(default_factory=dict)

    def add(self, pairs: Iterable[Tuple[str, str]]) -> None:
        for alias, kind in pairs:
            self.kinds.setdefault(alias, set()).add(kind)

    @classmethod
    def of(cls, *sources: Sequence[Tuple[str, str]]) -> "CatalogKinds":
        out = cls()
        for pairs in sources:
            out.add(pairs)
        return out

    def kind(self, alias: str) -> Optional[str]:
        """The kind of ``alias``, or ``None`` when the code does not say or says two."""
        found = self.kinds.get(alias.lower(), set())
        return next(iter(found)) if len(found) == 1 else None

    def conflicting(self, alias: str) -> List[str]:
        """The kinds the code sets ``alias`` to, when it sets more than one."""
        found = self.kinds.get(alias.lower(), set())
        return sorted(found) if len(found) > 1 else []
