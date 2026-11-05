# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Section mapper protocol + pass-through namespace for the Bitol ODPS provider.

Mirrors the design of :mod:`fluid_build.providers.odcs.mappers.base` — both
modules share the same accessor protocol via
:mod:`fluid_build.providers._mapper_common`. Per-level pass-through
namespaces:

- contract-level → ``metadata.odps_passthrough.*``
- per-expose    → ``expose.odps_passthrough.*``
- per-expect    → ``expect.odps_passthrough.*``
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from fluid_build.providers._mapper_common import (
    fluid_id,
    make_passthrough_helpers,
    resolve_status,
)
from fluid_build.util.contract import (
    derived_contract_id,
    explicit_expose_contract_id,
    expose_contract_id,
)

PASSTHROUGH_KEY = "odps_passthrough"

(
    metadata_passthrough,
    get_metadata_passthrough,
    expose_passthrough,
    get_expose_passthrough,
    expect_passthrough,
    get_expect_passthrough,
) = make_passthrough_helpers(PASSTHROUGH_KEY)


@dataclass
class ImportCtx:
    """Mutable context threaded through ODPS → FLUID mappers."""

    odps: Mapping[str, Any]
    fluid: MutableMapping[str, Any]
    logger: logging.Logger
    options: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExportCtx:
    """Mutable context threaded through FLUID → ODPS mappers."""

    fluid: Mapping[str, Any]
    odps: MutableMapping[str, Any]
    logger: logging.Logger
    options: Dict[str, Any] = field(default_factory=dict)


def contract_id_for_port(product_id: str, port_name: str) -> str:
    """Convention used by export + datamesh-manager: ``{productId}.{portName}``.

    Delegates to :func:`fluid_build.util.contract.derived_contract_id` rather
    than spelling the f-string again: ``fluid validate`` has to derive the same
    string to catch a pinned id that collides with a derived one, and two
    copies of the convention are two things to keep in step.
    """
    return derived_contract_id(product_id, port_name)


def contract_id_for_expose(
    expose: Mapping[str, Any],
    product_id: str,
    port_name: str,
    contract: Optional[Mapping[str, Any]] = None,
) -> str:
    """The contract id for one output port: the one the expose names, else derived.

    Deriving it unconditionally meant an id could not be pinned and an
    imported one did not survive: a port imported with a catalog's own
    contractId re-exported as ``{productId}.{portName}``, and renaming either
    half renamed the contract. The lookup is
    :func:`fluid_build.util.contract.expose_contract_id`, the same one the ODCS
    per-port render resolves through, which is what keeps the two ids equal and
    the fragments-mode linking invariant intact.

    ``contract`` is optional only because the output-port mapper runs
    exclusively inside :meth:`BitolOdpsProvider.render`, which rehydrates an
    imported contract's ``extensions.odps`` onto each expose before any mapper
    sees it — so the inline bucket is already populated on this path. Pass it
    when you have it; a caller outside that path that omits it would re-derive
    an imported id.
    """
    return expose_contract_id(
        contract if contract is not None else {"id": product_id},
        expose,
        expose_id=port_name,
    ) or contract_id_for_port(product_id, port_name)


__all__ = [
    "PASSTHROUGH_KEY",
    "metadata_passthrough",
    "get_metadata_passthrough",
    "expose_passthrough",
    "get_expose_passthrough",
    "expect_passthrough",
    "get_expect_passthrough",
    "ImportCtx",
    "ExportCtx",
    "fluid_id",
    "resolve_status",
    "contract_id_for_port",
    "contract_id_for_expose",
    "explicit_expose_contract_id",
    "expose_contract_id",
]
