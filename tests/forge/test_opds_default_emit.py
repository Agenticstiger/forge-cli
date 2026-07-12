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

"""OPDS default-emit + schema-validation round-trip (the card's headline gate).

**OPDS** here means the Linux Foundation / ODPI Open Data Product
*Specification* v4.1 (``providers/opds/``) — distinct from Bitol's Open Data
Product *Standard* (``odps-bitol``) and Bitol's Open Data *Contract* Standard
(``odcs``). These tests pin, end to end:

1. the OPDS emitter produces the conformant ``{schema, version, product}`` v4.1
   shape and it VALIDATES against the vendored ``opds-schema-v4.1.0.json``;
2. that emit preserves the source contract's identity (a round-trip check);
3. ``fluid generate artifacts`` emits ``opds/*.opds.json`` in the DEFAULT set
   and stage-4 ``validate artifacts`` dispatches it to ``validate_opds`` and
   passes it clean;
4. the deprecated ``--emit odps`` alias resolves to ``opds`` (never a separate
   ``odps/`` subdir);
5. ``validate_opds`` actually catches a non-conformant document.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from fluid_build.forge.core.artifact_fanout import run_fanout
from fluid_build.forge.core.artifact_validators import validate_artifacts, validate_opds
from fluid_build.loader import load_contract
from fluid_build.providers.opds.opds import OdpsProvider
from fluid_build.providers.opds.validator import validate_against_opds_schema

_HELLO_WORLD_CONTRACT = Path(__file__).parent.parent.parent / (
    "examples/01-hello-world/contract.fluid.yaml"
)


@pytest.fixture
def logger():
    return logging.getLogger("test.opds_default_emit")


def _opds_doc(contract: dict) -> dict:
    """Emit the bare on-disk OPDS document exactly as the fanout writes it.

    ``OdpsProvider.render`` returns the ``{..., artifacts: {...}}`` envelope;
    the file written to ``opds/<slug>.opds.json`` is the unwrapped ``artifacts``
    doc (see ``generate_standard._export_odps_v4_1``). Mirror that unwrap so the
    round-trip assertions test the *shipped* bytes.
    """
    rendered = OdpsProvider().render(contract)
    return rendered.get("artifacts", rendered) if isinstance(rendered, dict) else rendered


# ---------------------------------------------------------------------------
# Emitter conformance + schema validation
# ---------------------------------------------------------------------------


class TestOpdsEmitterIsSchemaValid:
    def test_bare_doc_has_v41_root_shape(self):
        doc = _opds_doc(load_contract(str(_HELLO_WORLD_CONTRACT)))
        assert set(doc.keys()) == {"schema", "version", "product"}

    def test_validates_against_vendored_schema_full_schema(self):
        doc = _opds_doc(load_contract(str(_HELLO_WORLD_CONTRACT)))
        valid, errors, validation_type = validate_against_opds_schema(doc, "4.1")
        # Must be the REAL JSON-Schema check, not the basic fallback — proving
        # the vendored opds-schema-v4.1.0.json resolved off disk.
        assert validation_type == "full_schema"
        assert valid, f"emitted OPDS doc failed schema validation: {errors}"


# ---------------------------------------------------------------------------
# Round-trip: the emit preserves the source contract's identity
# ---------------------------------------------------------------------------


class TestOpdsRoundTripFidelity:
    def test_product_identity_round_trips(self):
        contract = load_contract(str(_HELLO_WORLD_CONTRACT))
        doc = _opds_doc(contract)

        details = doc["product"]["details"]
        # Exactly one language block; default language code is 'en'.
        (lang_block,) = list(details.values())
        assert lang_block["productID"] == contract["id"]
        assert lang_block["name"] == contract.get("name", contract["id"])
        assert lang_block["description"] == contract.get("description", "")

    def test_fluid_extensions_are_preserved_under_x_fluid(self):
        """OPDS is export-only; the round-trip guarantee is that FLUID-native
        detail survives under ``product.x-fluid`` for a consumer to recover."""
        contract = load_contract(str(_HELLO_WORLD_CONTRACT))
        doc = _opds_doc(contract)
        assert "x-fluid" in doc["product"]
        # _legacy carries the flat identity mirror used by v4.0 readers.
        assert doc["product"]["_legacy"]["dataProductId"] == contract["id"]


# ---------------------------------------------------------------------------
# Default emit set + stage-4 validation (the actual pipeline)
# ---------------------------------------------------------------------------


class TestOpdsInDefaultEmit:
    def test_default_emit_writes_and_validates_opds(self, tmp_path, logger):
        contract = tmp_path / "contract.fluid.yaml"
        contract.write_text(_HELLO_WORLD_CONTRACT.read_text())
        out_dir = tmp_path / "art"

        run_fanout(contract, out_dir, emit_raw=None, manifest_path=None, logger=logger)

        # The OPDS artifact is emitted by DEFAULT (no --emit needed).
        opds_files = list((out_dir / "opds").glob("*.opds.json"))
        assert len(opds_files) == 1, f"expected one opds/*.opds.json; got {opds_files}"

        # And stage-4 validate-artifacts passes it clean (schema-valid).
        report = validate_artifacts(out_dir)
        assert report.status == "pass", [i.message for i in report.issues if i.severity == "error"]
        opds_errors = [i for i in report.issues if i.validator == "opds" and i.severity == "error"]
        assert opds_errors == [], opds_errors

    def test_deprecated_odps_alias_lands_in_opds_dir(self, tmp_path, logger):
        """``--emit odps`` (deprecated) must resolve to ``opds`` and write to the
        ``opds/`` subdir — never a separate ``odps/`` directory."""
        contract = tmp_path / "contract.fluid.yaml"
        contract.write_text(_HELLO_WORLD_CONTRACT.read_text())
        out_dir = tmp_path / "art"

        run_fanout(contract, out_dir, emit_raw="odps", manifest_path=None, logger=logger)

        assert (out_dir / "opds").is_dir()
        assert not (out_dir / "odps").exists()
        assert validate_artifacts(out_dir).status == "pass"


# ---------------------------------------------------------------------------
# validate_opds actually catches bad documents
# ---------------------------------------------------------------------------


class TestValidateOpds:
    def test_clean_doc_produces_no_issues(self):
        doc = _opds_doc(load_contract(str(_HELLO_WORLD_CONTRACT)))
        issues = validate_opds("opds/x.opds.json", json.dumps(doc).encode("utf-8"))
        assert issues == []

    def test_missing_product_is_flagged(self):
        broken = json.dumps({"schema": "x", "version": "4.1"}).encode("utf-8")
        issues = validate_opds("opds/x.opds.json", broken)
        assert any(i.severity == "error" and i.validator == "opds" for i in issues)

    def test_non_json_is_a_parse_error(self):
        issues = validate_opds("opds/x.opds.json", b"this: is not: json {{{")
        assert any(i.code == "OPDS-PARSE" for i in issues)
