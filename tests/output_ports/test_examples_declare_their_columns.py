# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Every example contract the MCP output port can serve declares its columns.

``sample`` and ``query_sql`` read only the columns ``contract.schema`` declares, for
every caller, and refuse an expose that declares none. An example whose binding a
gateway driver serves must therefore declare them, or it stops working once served.
Each example is read through forge's own loader, as ``fluid mcp output-port serve``
reads it, so a ``$ref`` is resolved first.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import pytest

from fluid_build._contract_loader import load_contract_with_overlay
from fluid_build.output_ports.mcp.drivers import _DRIVER_REGISTRY
from fluid_build.output_ports.mcp.drivers.base import UnsupportedBindingError, get_binding
from fluid_build.output_ports.mcp.query_compiler import declared_columns

pytestmark = pytest.mark.unit

_EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


def _contracts() -> Iterator[Tuple[Path, Dict[str, Any]]]:
    for path in sorted(_EXAMPLES.rglob("*.y*ml")):
        text = path.read_text(encoding="utf-8")
        if "fluidVersion" in text and "exposes" in text:
            yield path, load_contract_with_overlay(str(path), None, logging.getLogger(__name__))


def _servable_exposes() -> List[Tuple[str, Dict[str, Any]]]:
    found = []
    for path, contract in _contracts():
        for expose in contract.get("exposes") or []:
            if not isinstance(expose, dict) or not isinstance(expose.get("binding"), dict):
                continue
            try:
                platform, file_format, _ = get_binding(expose)
            except UnsupportedBindingError:
                continue  # a binding no driver reads is not served
            if (platform, file_format) in _DRIVER_REGISTRY:
                label = f"{path.relative_to(_EXAMPLES).as_posix()}:{expose.get('exposeId')}"
                found.append((label, expose))
    return found


_SERVABLE = _servable_exposes()


def test_the_scan_finds_the_examples_a_gateway_serves():
    assert len(_SERVABLE) >= 20, [label for label, _ in _SERVABLE]


@pytest.mark.parametrize(("label", "expose"), _SERVABLE, ids=[label for label, _ in _SERVABLE])
def test_every_servable_example_expose_declares_its_columns(label, expose):
    assert declared_columns(expose), label
