# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Ordered mapper pipeline for the Bitol ODPS provider.

Export order:
  - ``product`` establishes apiVersion/kind/id/status/name/version/description.
  - ``team`` adds the team object.
  - ``ports`` emits inputPorts/outputPorts; ``contractId`` follows
    ``{productId}.{portName}`` so per-port ODCS contracts emitted by the
    provider's :meth:`render` line up exactly.
  - ``support`` carries support/management-port pass-throughs.

Import order matches export. The provider drives full per-port ODCS
resolution in :meth:`import_contract` after this pipeline produces the
expose/expect stubs.
"""

from . import ports, product, support, team  # noqa: F401

EXPORT_PIPELINE = [product, team, ports, support]
IMPORT_PIPELINE = [product, team, ports, support]

__all__ = ["EXPORT_PIPELINE", "IMPORT_PIPELINE"]
