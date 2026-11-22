# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Foreign-config converters for ``fluid import <tool> <source>``.

Built-in sources (the names are :data:`registry.BUILTIN_IMPORTERS`):

  - ``meltano <project-dir>``    - converts ``meltano.yml`` + plugins
  - ``airbyte --workspace-id``   - converts an Airbyte workspace via REST
  - ``dlt --pipeline-name``      - converts a dlt pipeline state directory
  - ``singer <tap-cfg>``         - converts a single Singer tap+target pair
  - ``dbt <project-dir>``        - converts ``target/manifest.json``
  - ``schema-registry <subject>`` - a Confluent or Glue subject -> a consumer contract
  - ``airflow <dags-dir>``       - Airflow DAG files -> products, read without importing Airflow
  - ``pyspark <jobs-dir>``       - PySpark jobs -> one product per output table or topic

Packages add sources through the ``fluid_build.importers`` entry-point group (see
:mod:`fluid_build.cli.import_workflow.registry`).

Each converter returns a contract dict + a translation report describing
what mapped 1:1, what required defaults, and what the user still needs
to fill in.

Importing this package loads the registry only; an importer's module loads when its tool is
used, or when its class is read from here (``from fluid_build.cli.import_workflow import
DbtManifestImporter``).
"""

from __future__ import annotations

import importlib
from typing import Any

from .registry import (
    BUILTIN_IMPORTERS,
    IMPORTERS,
    Importer,
    ImporterChoices,
    ImportReport,
    coerce_report,
    get_importer,
    importer_names,
    register_importer,
)

__all__ = [
    "AirbyteImporter",
    "AirflowImporter",
    "BUILTIN_IMPORTERS",
    "DbtManifestImporter",
    "DltImporter",
    "IMPORTERS",
    "Importer",
    "ImporterChoices",
    "ImportReport",
    "MeltanoImporter",
    "PySparkImporter",
    "SchemaRegistryImporter",
    "SingerImporter",
    "coerce_report",
    "get_importer",
    "importer_names",
    "register_importer",
]

# Importer classes, resolved from their module on first read (PEP 562), so naming them here
# does not import them.
_CLASSES = {
    "AirbyteImporter": "airbyte",
    "AirflowImporter": "airflow",
    "PySparkImporter": "pyspark",
    "DbtManifestImporter": "dbt",
    "DltImporter": "dlt",
    "MeltanoImporter": "meltano",
    "SchemaRegistryImporter": "schema_registry",
    "SingerImporter": "singer",
}


def __getattr__(name: str) -> Any:
    module = _CLASSES.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(f"{__name__}.{module}"), name)
    globals()[name] = value
    return value
