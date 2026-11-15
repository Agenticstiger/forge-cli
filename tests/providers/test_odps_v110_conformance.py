# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Bitol ODPS v1.1.0, item by item: what forge supports and what it does not yet.

Bitol released v1.1.0 on 2026-09-08. This module is the ledger behind whatever
forge says about that release. A passing test is something forge does today. A
strict xfail is a known gap: its reason names the field and the RFC, and when
someone closes the gap the xfail turns into a failure (XPASS) until its marker
is deleted, so the ledger cannot go stale quietly.

Where forge stands, as of this file:

* It emits v1.1.0 on request (``ODPS_API_VERSION=v1.1.0``, or
  ``fluid odps-bitol export --api-version v1.1.0``) and validates every
  document it writes against the vendored schema. v1.0.0 stays the default: the
  emitted apiVersion is what existing consumers parse, so the default moves with
  a schema bump, not silently (``validation.py::DEFAULT_API_VERSION``, pinned by
  ``test_odps_v11_type.py``). Nothing here changes that.
* It reads v1.1.0 documents: all five official examples validate and import.
* It does not yet carry every v1.1.0 field through import then export. The
  probes below say which fields it carries and which it loses.

The fixtures in ``tests/fixtures/odps/upstream-v1.1.0/`` are the five files of
``docs/examples`` at tag v1.1.0 (commit 04995037fc003ead1da27f65a40a09eb0664b591
of bitol-io/open-data-product-standard), byte for byte. Refresh them from a tag,
never from a branch.

Borrowed pattern: Bitol's own ``scripts/validate-examples.sh`` validates every
example against the schema of the same tag. The round trip is ours: no other
tool was found that does one.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict, Sequence, Tuple

import pytest
import yaml

from fluid_build.providers.base import ProviderError
from fluid_build.providers.odps_standard.provider import BitolOdpsProvider
from fluid_build.providers.odps_standard.validation import (
    load_schema,
    schema_for_document,
    validate,
)

pytestmark = pytest.mark.unit

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "odps" / "upstream-v1.1.0"

CLEAN_EXAMPLES = ("simple-data-product", "variables", "vendor-custom-properties")
ALL_EXAMPLES = (
    "customer-data-product",
    "deprecated-data-product",
    *CLEAN_EXAMPLES,
)

Steps = Tuple[Any, ...]
MISSING = object()


def _provider() -> BitolOdpsProvider:
    """A provider that emits v1.1.0, strictly, whatever the environment says."""
    provider = BitolOdpsProvider()
    provider.api_version = "v1.1.0"
    provider.schema = load_schema("v1.1.0")
    provider.strict_validation = True
    provider.include_custom_properties = True
    return provider


def _round_trip(source: Any, base_path: Path) -> Dict[str, Any]:
    """Import an ODPS document (path or dict), export it again, return the product.

    ``lenient`` because the examples name ODCS contracts that are not shipped
    next to them; ``base_path`` keeps the resolver from finding anything else.
    """
    provider = _provider()
    fluid = provider.import_contract(source, base_path=base_path, lenient=True)
    return provider.render(fluid)["product"]


def _load(name: str) -> Dict[str, Any]:
    return yaml.safe_load((EXAMPLES_DIR / f"{name}.odps.yaml").read_text(encoding="utf-8"))


class KnownGap(Exception):
    """The one failure a strict xfail in this ledger may absorb, and nothing else.

    ``xfail(raises=AssertionError)`` would also absorb an assert inside the provider, and
    ``raises=ProviderError`` any refusal, a probe that is not valid ODPS included. A gap test
    raises this itself, and only after it has seen the exact symptom it documents.
    """


def _round_trip_or_gap(
    source: Any, base_path: Path, *, refusal: str, reason: str
) -> Dict[str, Any]:
    """Round trip, where the one refusal named by ``refusal`` is the documented gap."""
    try:
        return _round_trip(source, base_path)
    except ProviderError as exc:
        if refusal in str(exc):
            raise KnownGap(reason) from exc
        raise


# ---------------------------------------------------------------------------
# The five official examples
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ALL_EXAMPLES)
def test_example_validates_against_the_vendored_v110_schema(name: str) -> None:
    doc = _load(name)

    assert doc["apiVersion"] == "v1.1.0"
    validate(doc, schema_for_document(doc))


@pytest.mark.parametrize("name", ALL_EXAMPLES)
def test_example_imports(name: str, tmp_path: Path) -> None:
    fluid = _provider().import_contract(
        EXAMPLES_DIR / f"{name}.odps.yaml", base_path=tmp_path, lenient=True
    )

    assert fluid["kind"] == "DataProduct"
    assert fluid["id"] == _load(name)["id"]


@pytest.mark.parametrize("name", CLEAN_EXAMPLES)
def test_example_round_trips_unchanged(name: str, tmp_path: Path) -> None:
    product = _round_trip(EXAMPLES_DIR / f"{name}.odps.yaml", tmp_path)

    assert product == _load(name)


_DEPRECATED_EXAMPLE_REASON = (
    "RFC-0051: `deprecated` on an input or output port is lost on export: import keeps it "
    "only in the verbatim source copy and the port mapper does not replay it"
)


def _without_port_deprecated(doc: Dict[str, Any]) -> Dict[str, Any]:
    """The document minus the port-level `deprecated` flags: what forge writes today."""
    doc = copy.deepcopy(doc)
    for kind in ("inputPorts", "outputPorts"):
        for port in doc.get(kind) or []:
            port.pop("deprecated", None)
    return doc


@pytest.mark.xfail(strict=True, raises=KnownGap, reason=_DEPRECATED_EXAMPLE_REASON)
def test_deprecated_example_round_trips_unchanged(tmp_path: Path) -> None:
    expected = _load("deprecated-data-product")
    product = _round_trip(EXAMPLES_DIR / "deprecated-data-product.odps.yaml", tmp_path)

    if product != expected:
        # Exactly those flags and nothing else, or this is a new defect and not the gap.
        assert product == _without_port_deprecated(expected)
        raise KnownGap(_DEPRECATED_EXAMPLE_REASON)


_CUSTOMER_EXAMPLE_REASON = (
    "two output ports share the name `rawtransactions` (versions 1.0.0 and 2.0.0) and an "
    "expose id is unique, so export refuses; import keeps one pass-through entry per id and "
    "`fluid validate` accepts the duplicate. Input ports that share a name collapse to one "
    "too (see the same-name input probe below)"
)


@pytest.mark.xfail(strict=True, raises=KnownGap, reason=_CUSTOMER_EXAMPLE_REASON)
def test_customer_example_round_trips_unchanged(tmp_path: Path) -> None:
    product = _round_trip_or_gap(
        EXAMPLES_DIR / "customer-data-product.odps.yaml",
        tmp_path,
        refusal="Duplicate output port name",
        reason=_CUSTOMER_EXAMPLE_REASON,
    )

    # Reached only once the refusal is gone; whatever still differs is then a new ledger entry.
    assert product == _load("customer-data-product")


# ---------------------------------------------------------------------------
# One probe per v1.1.0 field: plant it in a minimal valid document, round trip
# ---------------------------------------------------------------------------


def _base() -> Dict[str, Any]:
    """A minimal v1.1.0 product with one of every port kind and the shared lists."""
    return {
        "apiVersion": "v1.1.0",
        "kind": "DataProduct",
        "id": "conformance.probe",
        "name": "Conformance probe",
        "version": "1.0.0",
        "status": "active",
        "inputPorts": [{"name": "source", "version": "1.0.0", "contractId": "upstream.source"}],
        "outputPorts": [
            {"name": "orders", "version": "1.0.0", "contractId": "conformance.probe.orders"}
        ],
        "managementPorts": [{"name": "admin", "content": "control", "type": "rest"}],
        "support": [{"channel": "help", "url": "https://example.test/help"}],
        "team": {"name": "probe team", "members": [{"username": "member"}]},
    }


def _plant(path: Steps, value: Any) -> Dict[str, Any]:
    doc = _base()
    node: Any = doc
    for step in path[:-1]:
        node = node[step]
    node[path[-1]] = copy.deepcopy(value)
    # A probe that is not valid v1.1.0 would pass for the wrong reason.
    validate(doc, schema_for_document(doc))
    return doc


def _read(doc: Any, path: Steps) -> Any:
    node = doc
    for step in path:
        try:
            node = node[step]
        except (KeyError, IndexError, TypeError):
            return MISSING
    return node


def _pid(path: Steps) -> str:
    return ".".join(f"[{p}]" if isinstance(p, int) else str(p) for p in path).replace(".[", "[")


def _vendor_props(prop_id: str) -> Sequence[Dict[str, str]]:
    return [{"id": prop_id, "property": "owner", "value": "x", "vendor": "acme"}]


def _definitions(def_id: str) -> Sequence[Dict[str, str]]:
    return [{"id": def_id, "type": "businessDefinition", "url": "https://example.test/d"}]


_CONTEXT: Dict[str, Any] = {
    "instructions": "Prefer the orders port for reporting.",
    "verifiedStatements": [{"id": "vs", "question": "How many orders last month?"}],
    "constraints": [{"id": "c", "constraint": "Never write to this port."}],
}
_SYNONYMS: Sequence[Dict[str, str]] = [{"id": "syn", "synonym": "Sales", "locale": "en-US"}]

# What forge carries through import then export today.
SUPPORTED = (
    # RFC-0029
    (("type",), "aggregate"),
    # RFC-0035: `vendor` on customProperties, wherever the property list is
    # carried verbatim.
    (("customProperties",), _vendor_props("cp")),
    (("inputPorts", 0, "customProperties"), _vendor_props("icp")),
    (("outputPorts", 0, "customProperties"), _vendor_props("ocp")),
    (("managementPorts", 0, "customProperties"), _vendor_props("mcp")),
    # RFC-0050: `${VAR_NAME}` and `${VAR_NAME:-default}` stay exactly as written.
    (("tenant",), "${TENANT:-RetailCorp}"),
    (("description",), {"purpose": "Orders for ${TARGET_ENVIRONMENT:-prod} consumers."}),
    (("customProperties",), [{"property": "path", "value": "s3://${DATA_BUCKET}/orders/"}]),
    # RFC-0061: tags, customProperties and authoritativeDefinitions on sbom, and
    # an sbom entry with no url.
    (
        ("outputPorts", 0, "sbom"),
        [
            {
                "id": "sbom",
                "type": "external",
                "tags": ["runtime"],
                "customProperties": _vendor_props("sbomcp"),
                "authoritativeDefinitions": _definitions("sbomad"),
            },
            {"type": "external"},
        ],
    ),
    # RFC-0051 on a management port, and `id` on the lists that are carried verbatim.
    (("managementPorts", 0, "deprecated"), True),
    (("managementPorts", 0, "id"), "mp"),
    (("authoritativeDefinitions",), _definitions("ad")),
    (("support", 0, "id"), "sup"),
    (("team", "members", 0, "id"), "member-id"),
)

# Import does keep the whole source document (extensions.odps.metadata.source), but nothing
# on the export side reads that copy, and the mappers replay only the keys they know.
_LOST = "lost on export: import keeps it only in the verbatim source copy"
_NO_HOME = f"{_LOST}, and the product mapper does not replay it"
_NO_PORT_HOME = f"{_LOST}, and the port mapper does not replay it"

# What forge loses. Each one is a strict xfail: it must fail, and for this reason.
GAPS = (
    # RFC-0038 and RFC-0041, product and output port level.
    (("context",), _CONTEXT, f"RFC-0038 context at the product level, {_NO_HOME}"),
    (("outputPorts", 0, "context"), _CONTEXT, f"RFC-0038 context on a port, {_NO_PORT_HOME}"),
    (("synonyms",), _SYNONYMS, f"RFC-0041 synonyms at the product level, {_NO_HOME}"),
    (("outputPorts", 0, "synonyms"), _SYNONYMS, f"RFC-0041 synonyms on a port, {_NO_PORT_HOME}"),
    # RFC-0051 at the product level and on input and output ports.
    (("deprecated",), True, f"RFC-0051 deprecated at the product level, {_NO_HOME}"),
    (
        ("inputPorts", 0, "deprecated"),
        True,
        f"RFC-0051 deprecated on an input port, {_NO_PORT_HOME}",
    ),
    (
        ("outputPorts", 0, "deprecated"),
        True,
        f"RFC-0051 deprecated on an output port, {_NO_PORT_HOME}",
    ),
    # Optional `id` on the port objects themselves.
    (("inputPorts", 0, "id"), "in", f"optional id on an input port, {_NO_PORT_HOME}"),
    (("outputPorts", 0, "id"), "out", f"optional id on an output port, {_NO_PORT_HOME}"),
    # Not new in v1.1.0: a port is named, and one name can carry several versions. Output
    # ports with a shared name are refused (see the customer example); input ports are
    # silently collapsed to the first, because the export keeps one input port per name.
    (
        ("inputPorts",),
        [
            {"name": "source", "version": "1.0.0", "contractId": "upstream.source"},
            {"name": "source", "version": "2.0.0", "contractId": "upstream.source.v2"},
        ],
        "two input ports that share a name (versions 1.0.0 and 2.0.0) collapse to the first: "
        "the export keeps one input port per name (mappers/ports.py, the expects path)",
    ),
)


def test_the_document_every_probe_starts_from_round_trips_unchanged(tmp_path: Path) -> None:
    """Control: a probe then measures the field it plants and nothing else."""
    base = _base()
    validate(base, schema_for_document(base))

    assert _round_trip(base, tmp_path) == base


@pytest.mark.parametrize(
    ("path", "value"),
    SUPPORTED,
    ids=[_pid(path) + f"#{i}" for i, (path, _) in enumerate(SUPPORTED)],
)
def test_field_survives_import_then_export(path: Steps, value: Any, tmp_path: Path) -> None:
    product = _round_trip(_plant(path, value), tmp_path)

    assert _read(product, path) == value


@pytest.mark.parametrize(
    ("path", "value", "reason"),
    [
        pytest.param(
            path,
            value,
            reason,
            id=_pid(path),
            marks=pytest.mark.xfail(strict=True, raises=KnownGap, reason=reason),
        )
        for path, value, reason in GAPS
    ],
)
def test_field_is_lost_between_import_and_export(
    path: Steps, value: Any, reason: str, tmp_path: Path
) -> None:
    # An error in _plant or in the round trip is a failure here, not the documented gap.
    product = _round_trip(_plant(path, value), tmp_path)

    if _read(product, path) != value:
        raise KnownGap(reason)


_VENDOR_IN_DESCRIPTION_REASON = (
    "RFC-0035 `vendor` inside description.customProperties: the description is copied into "
    "each per-port ODCS contract and ODCS v3.1.0 has no `vendor` (v3.2.0 added it), so the "
    "sibling contract fails validation and the whole export is refused"
)


@pytest.mark.xfail(strict=True, raises=KnownGap, reason=_VENDOR_IN_DESCRIPTION_REASON)
def test_vendor_inside_description_survives_import_then_export(tmp_path: Path) -> None:
    path: Steps = ("description",)
    value = {"purpose": "Orders.", "customProperties": _vendor_props("dcp")}

    product = _round_trip_or_gap(
        _plant(path, value),
        tmp_path,
        refusal="'vendor' was unexpected",
        reason=_VENDOR_IN_DESCRIPTION_REASON,
    )

    assert _read(product, path) == value


# ---------------------------------------------------------------------------
# Accepted differences: the output is valid v1.1.0, but not the input's twin
# ---------------------------------------------------------------------------


def test_ports_that_carry_only_a_name_come_back_as_valid_v110(tmp_path: Path) -> None:
    """v1.1.0 relaxed ports to `name` alone. Forge accepts that and writes a fuller port.

    An output port links to an ODCS contract that forge writes beside the product, so
    the port gets a contractId and a version of forge's choosing, which the input did
    not have. The document stays valid and the names survive; the extra values are a
    deliberate default, not a loss.
    """
    doc = _base()
    doc["inputPorts"] = [{"name": "only-name-in"}]
    doc["outputPorts"] = [{"name": "only-name-out"}]
    validate(doc, schema_for_document(doc))

    product = _round_trip(doc, tmp_path)

    validate(product, load_schema("v1.1.0"))
    assert [p["name"] for p in product["inputPorts"]] == ["only-name-in"]
    assert [p["name"] for p in product["outputPorts"]] == ["only-name-out"]


def test_a_product_with_no_status_comes_back_as_valid_v110(tmp_path: Path) -> None:
    """v1.1.0 stopped requiring `status`. Forge's contracts always have a lifecycle
    state, so the export writes one; the document stays valid."""
    doc = _base()
    del doc["status"]
    validate(doc, schema_for_document(doc))

    product = _round_trip(doc, tmp_path)

    validate(product, load_schema("v1.1.0"))
    assert product["status"]
