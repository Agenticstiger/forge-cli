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

"""``fluid odcs import`` must emit a contract ``fluid validate`` accepts.

The importer used to print ``✓ Imported`` for files its own validator then
rejected. Every fixture below is one we did *not* write to suit the mappers:

* ``bitol-full-example.odcs.yaml`` — the ODCS project's own reference document,
  vendored verbatim. An importer's job is third-party documents, so a fixture
  we authored can only prove the importer agrees with itself. This one alone
  surfaced two live defects (``slaProperties`` with units, property-level
  ``classification``).
* the pre-existing ``contract-{minimal,full,edge}.yaml`` fixtures — including
  ``contract-edge.yaml``, the input behind the original "18 schema errors".

The failure mode these guard against is specific: the mappers wrote values into
FLUID fields whose schema forbids them — a bare ``4`` into ``freshnessSLO``
(``$defs/isoDuration``), an integer into ``qos.labels`` (string-valued),
``classification`` onto the closed ``$defs/column`` — and the CLI reported
success anyway.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from fluid_build.providers.odcs import OdcsProvider
from fluid_build.providers.odcs.mappers import sla
from fluid_build.schema_manager import FluidSchemaManager

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
ODCS_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "odcs"


def _violations(contract: Dict[str, Any]) -> List[str]:
    result = FluidSchemaManager().validate_contract(contract, offline_only=True)
    return [] if result.is_valid else list(result.errors)


def _fixture_files() -> List[Path]:
    return sorted(ODCS_FIXTURES.glob("*.yaml"))


# ---------------------------------------------------------------------------
# The headline defect: import reports success, writes an invalid contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _fixture_files(), ids=lambda p: p.name)
def test_every_odcs_fixture_imports_to_a_valid_fluid_contract(path: Path) -> None:
    imported = OdcsProvider().import_contract(yaml.safe_load(path.read_text()))
    violations = _violations(imported)
    assert not violations, f"{path.name} imported to an INVALID FLUID contract:\n" + "\n".join(
        f"  {v}" for v in violations
    )


def test_the_official_bitol_example_is_actually_present_and_third_party() -> None:
    """Guard the guard: if the vendored fixture ever goes missing or gets
    trimmed to whatever the mappers happen to support, the parametrized test
    above silently stops proving anything."""
    path = ODCS_FIXTURES / "bitol-full-example.odcs.yaml"
    assert path.exists(), "vendored ODCS reference document is missing"
    doc = yaml.safe_load(path.read_text())
    # The exact shapes that caught the bugs — these must stay in the fixture.
    sla_props = {p["property"]: p for p in doc["slaProperties"]}
    assert sla_props["latency"]["unit"] == "d"
    assert sla_props["retention"]["unit"] == "y"
    assert any(
        "classification" in prop for obj in doc["schema"] for prop in obj.get("properties", [])
    )


# ---------------------------------------------------------------------------
# slaProperties → qos: units carried, nothing silently discarded
# ---------------------------------------------------------------------------


def _qos_for(sla_properties: List[Dict[str, Any]]) -> Dict[str, Any]:
    odcs = {
        "apiVersion": "v3.1.0",
        "kind": "DataContract",
        "id": "sla.probe",
        "version": "1.0.0",
        "status": "active",
        "schema": [
            {
                "name": "t",
                "logicalType": "object",
                "physicalType": "table",
                "properties": [{"name": "c", "logicalType": "string"}],
            }
        ],
        "slaProperties": sla_properties,
    }
    imported = OdcsProvider().import_contract(odcs)
    assert not _violations(imported), _violations(imported)
    return imported["exposes"][0].get("qos", {})


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        (4, "d", "P4D"),
        (4, "h", "PT4H"),
        (1, "day", "P1D"),
        (2, "weeks", "P2W"),
        (3, "y", "P3Y"),
        (30, "minutes", "PT30M"),
        (45, "s", "PT45S"),
        ("15", "min", "PT15M"),
    ],
)
def test_sla_unit_reaches_the_duration(value: Any, unit: str, expected: str) -> None:
    """``{property: frequency, value: 4, unit: h}`` used to become
    ``freshnessSLO: '4'`` — unit dropped, value stringified, and the result did
    not match ``$defs/isoDuration``."""
    assert (
        _qos_for([{"property": "frequency", "value": value, "unit": unit}])["freshnessSLO"]
        == expected
    )


@pytest.mark.parametrize(
    ("value", "unit"),
    [
        (4, "m"),  # ambiguous: minutes in a time part, months in a date part
        (4.5, "h"),  # the grammar is integer-only
        (-1, "d"),
        (True, "d"),  # bool is an int subclass; must not become P1D
        ("soon", "d"),
        (4, None),  # no unit at all
        (4, "parsecs"),
    ],
)
def test_unconvertible_sla_becomes_a_label_not_an_invalid_duration(value: Any, unit: Any) -> None:
    """The rule is "only write a constrained field when the value satisfies the
    constraint". Anything else lands in ``labels`` — legal, visible, and still
    lossless via the verbatim ``slaProperties`` pass-through."""
    prop: Dict[str, Any] = {"property": "frequency", "value": value}
    if unit is not None:
        prop["unit"] = unit
    qos = _qos_for([prop])
    assert "freshnessSLO" not in qos
    assert qos["labels"], "the value was neither converted nor preserved as a label"


def test_an_ambiguous_unit_is_refused_rather_than_guessed() -> None:
    """``m`` really is ambiguous, and guessing rescales the SLA ~44,000×.
    Pinned as its own case because "it happens to fail" and "we deliberately
    refuse it" are different guarantees."""
    assert sla._as_iso_duration(4, "m") is None
    assert sla._as_iso_duration(4, "min") == "PT4M"
    assert sla._as_iso_duration(4, "mo") == "P4M"


def test_an_already_formed_duration_survives_verbatim() -> None:
    """The FLUID-native leg: ``qos.freshnessSLO: PT15M`` exports as
    ``{property: interval, value: PT15M}`` with no unit and must come back
    unchanged."""
    assert _qos_for([{"property": "interval", "value": "PT15M"}])["freshnessSLO"] == "PT15M"


def test_latency_and_frequency_no_longer_collide() -> None:
    """Both used to map onto ``freshnessSLO`` via ``setdefault``, so whichever
    came second was silently discarded. The official Bitol example carries both
    plus ``retention``."""
    qos = _qos_for(
        [
            {"property": "latency", "value": 4, "unit": "d"},
            {"property": "frequency", "value": 1, "unit": "d"},
            {"property": "retention", "value": 3, "unit": "y"},
        ]
    )
    assert qos["latencyP95"] == "P4D"
    assert qos["freshnessSLO"] == "P1D"
    # retention is not a freshness concept and FLUID qos has no field for it,
    # so it must still be visible somewhere rather than dropped.
    assert qos["labels"]["retention:y"] == "3"


def test_a_repeated_property_lands_in_a_label_rather_than_disappearing() -> None:
    """ODCS allows repeats — its own reference document carries two
    ``timeOfAvailability`` entries. The first property to fill a qos field keeps
    it; the rest must still show up somewhere."""
    qos = _qos_for(
        [
            {"property": "availability", "value": 0.999},
            {"property": "availability", "value": 0.95},
            {"property": "interval", "value": 1, "unit": "d"},
            {"property": "interval", "value": 7, "unit": "d"},
        ]
    )
    assert qos["availability"] == "99.9%"
    assert qos["freshnessSLO"] == "P1D"
    assert qos["labels"]["availability"] == "0.95"
    assert qos["labels"]["interval:d"] == "7"


def test_every_qos_label_value_is_a_string() -> None:
    """``$defs/labels`` is ``additionalProperties: {type: string}``; the
    importer wrote raw ints (``errorRate:count: 10``), which is exactly the two
    remaining errors on ``contract-edge.yaml``."""
    qos = _qos_for(
        [
            {"property": "errorRate", "value": 10, "unit": "count"},
            {"property": "maxStaleness", "value": 24, "unit": "h"},
            {"property": "enabled", "value": True},
        ]
    )
    assert all(isinstance(v, str) for v in qos["labels"].values()), qos["labels"]


@pytest.mark.parametrize("value", ["not-a-number", 9.5, -1])
def test_unrepresentable_availability_does_not_become_an_invalid_qos_field(
    value: Any,
) -> None:
    """``$defs/availabilityPct`` needs two leading digits and a ``%``; ``9.5``
    and a free-text value cannot be spelled in it."""
    qos = _qos_for([{"property": "availability", "value": value}])
    assert "availability" not in qos


def test_normal_availability_still_works() -> None:
    """The neighbouring behaviour: the common cases must keep working exactly
    as before, including the fraction → percent conversion."""
    assert _qos_for([{"property": "availability", "value": 0.995}])["availability"] == "99.5%"
    assert _qos_for([{"property": "availability", "value": 99.5}])["availability"] == "99.5%"
    assert _qos_for([{"property": "availability", "value": "99.9%"}])["availability"] == "99.9%"


# ---------------------------------------------------------------------------
# Field-level fidelity: businessName and classification
# ---------------------------------------------------------------------------


def _minimal_fluid_with_column(extra: Dict[str, Any]) -> Dict[str, Any]:
    column = {"name": "CUSTOMER_KEY", "type": "STRING", "required": True}
    column.update(extra)
    return {
        "fluidVersion": FluidSchemaManager.latest_bundled_version(),
        "kind": "DataProduct",
        "id": "gold.retail.probe",
        "name": "probe",
        "metadata": {"owner": {"team": "data-platform"}},
        "exposes": [
            {
                "exposeId": "customers",
                "kind": "table",
                "binding": {
                    "platform": "snowflake",
                    "format": "snowflake_table",
                    "location": {"database": "FLUID_TEST", "schema": "S", "table": "C"},
                },
                "contract": {"schema": [column]},
            }
        ],
    }


def test_hand_written_business_name_reaches_odcs_and_comes_back() -> None:
    """``businessName`` is first-class in BOTH specs (ODCS v3.1.0
    SchemaProperty.businessName, FLUID ``$defs/column.businessName``) yet the
    export side only ever read ``business_name`` out of the pass-through — a key
    that exists solely on an already-imported contract. A hand-written column's
    businessName reached neither the ODCS document nor the extras blob."""
    source = _minimal_fluid_with_column({"businessName": "Customer Key"})
    assert not _violations(source), "the probe itself must be a valid FLUID contract"

    provider = OdcsProvider()
    odcs = provider.render(source)
    prop = odcs["schema"][0]["properties"][0]
    assert prop["businessName"] == "Customer Key", "businessName never reached the ODCS document"

    back = provider.import_contract(odcs)
    assert back["exposes"][0]["contract"]["schema"][0]["businessName"] == "Customer Key"


def test_shipped_contract_keeps_all_its_business_names() -> None:
    """The repo's own contract, which lost three of them."""
    path = REPO_ROOT / "examples" / "mcp-output-port" / "contract.fluid.yaml"
    if not path.exists():
        pytest.skip("example contract not present")
    source = yaml.safe_load(path.read_text())

    expected = [
        fld["businessName"]
        for expose in source["exposes"]
        for fld in expose.get("contract", {}).get("schema", [])
        if fld.get("businessName")
    ]
    assert expected, "fixture no longer exercises businessName"

    odcs = OdcsProvider().render(source)
    published = [
        prop["businessName"]
        for obj in odcs["schema"]
        for prop in obj.get("properties", [])
        if prop.get("businessName")
    ]
    assert sorted(published) == sorted(expected)


def test_classification_round_trips_without_landing_on_the_closed_column() -> None:
    """FLUID ``$defs/column`` is ``additionalProperties: false`` and declares no
    ``classification``; ODCS puts one on every property in its own reference
    document. It rides in the pass-through, like ``quality`` already did."""
    provider = OdcsProvider()
    odcs = {
        "apiVersion": "v3.1.0",
        "kind": "DataContract",
        "id": "cls.probe",
        "version": "1.0.0",
        "status": "active",
        "schema": [
            {
                "name": "t",
                "logicalType": "object",
                "physicalType": "table",
                "properties": [
                    {"name": "email", "logicalType": "string", "classification": "restricted"}
                ],
            }
        ],
    }
    imported = provider.import_contract(odcs)
    assert not _violations(imported), _violations(imported)
    assert "classification" not in imported["exposes"][0]["contract"]["schema"][0]
    # ...and it is still there on the way back out.
    assert provider.render(imported)["schema"][0]["properties"][0]["classification"] == "restricted"


# ---------------------------------------------------------------------------
# The CLI stops reporting success for an invalid import
# ---------------------------------------------------------------------------


def test_cli_import_exits_non_zero_when_the_output_is_not_valid_fluid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The mappers are fixed, but the *silent pass* is what made those bugs
    expensive. Forcing a bad import proves the guard is not a no-op."""
    from fluid_build.cli import odcs as odcs_cli

    monkeypatch.setattr(
        OdcsProvider,
        "import_contract",
        lambda self, src: {"fluidVersion": "0.7.5", "kind": "DataProduct", "id": "x"},
    )
    out = tmp_path / "bad.fluid.yaml"
    args = argparse.Namespace(
        odcs_file=str(ODCS_FIXTURES / "contract-minimal.yaml"),
        output=str(out),
        format="yaml",
    )
    rc = odcs_cli._run_odcs_import(args)

    assert rc == 1, "an invalid import must not report success"
    captured = capsys.readouterr()
    # Console wraps at terminal width, so compare on collapsed whitespace.
    said = " ".join((captured.out + captured.err).split())
    assert "Not a valid FLUID contract" in said
    assert "✓ Imported" not in said, "must not also claim success"
    assert out.exists(), "the file should still be written so it can be inspected"


def test_cli_import_still_reports_success_for_a_good_document(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The neighbouring behaviour: the guard must not turn working imports into
    failures."""
    from fluid_build.cli import odcs as odcs_cli

    out = tmp_path / "good.fluid.yaml"
    args = argparse.Namespace(
        odcs_file=str(ODCS_FIXTURES / "bitol-full-example.odcs.yaml"),
        output=str(out),
        format="yaml",
    )
    assert odcs_cli._run_odcs_import(args) == 0
    assert "✓ Imported" in capsys.readouterr().out
    assert not _violations(yaml.safe_load(out.read_text()))
