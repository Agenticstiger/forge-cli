# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Foreign-config converters for ``fluid import <tool> <config>``.

Five sources covered:

  - ``meltano <project-dir>``    — converts ``meltano.yml`` + plugins
  - ``airbyte --workspace-id``   — converts an Airbyte workspace via REST
  - ``dlt --pipeline-name``      — converts a dlt pipeline state directory
  - ``singer <tap-cfg>``         — converts a single Singer tap+target pair
  - ``dbt <project-dir>``        — converts ``target/manifest.json``

Each converter returns a contract dict + a translation report describing
what mapped 1:1, what required defaults, and what the user still needs
to fill in.
"""

from __future__ import annotations

from .airbyte import AirbyteImporter
from .dbt import DbtManifestImporter
from .dlt import DltImporter
from .meltano import MeltanoImporter
from .registry import IMPORTERS, Importer, ImportReport, get_importer, register_importer
from .singer import SingerImporter

__all__ = [
    "AirbyteImporter",
    "DbtManifestImporter",
    "DltImporter",
    "IMPORTERS",
    "Importer",
    "ImportReport",
    "MeltanoImporter",
    "SingerImporter",
    "get_importer",
    "register_importer",
]


# Auto-registry.
register_importer("meltano", MeltanoImporter())
register_importer("airbyte", AirbyteImporter())
register_importer("dlt", DltImporter())
register_importer("singer", SingerImporter())
register_importer("dbt", DbtManifestImporter())
