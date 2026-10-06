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

"""An LF-tag association without a tag definition in the contract emits a valid module.

``binding.governance.lakeFormation.tags`` associates LF-tags with the table;
``governance.lakeFormation.tagDefinitions`` (top level) is what makes the module
CREATE the tag. A binding that names a tag the contract does not define (the tag
is owned by a platform bootstrap, the account-wide ontology) used to get a
``depends_on`` on an ``aws_lakeformation_lf_tag`` the module never declares, so
``tofu validate`` failed ``Reference to undeclared resource``. Such a key is now a
platform-owned tag: associated as written, with no edge, no warning and no refusal.
A key the contract defines names its tag by reference,
``key = aws_lakeformation_lf_tag.<name>.key``, the provider's documented pattern, and
the association carries no ``depends_on``.

What the module cannot create or associate as declared is refused, at emit and by
``fluid validate`` with the same message: a value the contract's definition of the
tag does not allow, a definition with no values, two definitions that are one
resource name, and tags on a binding that names no Glue table. The schemas say so,
in every version that carries the fields.

The tests marked ``tofu`` need ``tofu`` on PATH and the provider from the registry
(no AWS credentials), like ``test_iac_aws_validate.py``; they skip without it. The
apply and plan against moto are in ``test_iac_lakeformation_tags_moto.py``.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping

import pytest

import fluid_build
from fluid_build.iac import build_module, runner
from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.governance_validation import validate_governance
from fluid_build.iac.providers.aws import AwsIacPlugin

pytestmark = [pytest.mark.unit, pytest.mark.provider, pytest.mark.aws]

CID = "gold_sales_orders"
ASSOC = f"{CID}_lf_tags_orders"
TAG_REF = re.compile(r"^\$\{(aws_lakeformation_lf_tag\.[A-Za-z0-9_]+)\.key\}$")
SCHEMAS = Path(fluid_build.__file__).resolve().parent / "schemas"


def _contract(tags: Mapping[str, str], definitions: Mapping[str, List[str]] | None = None):
    contract: Dict[str, Any] = {
        "id": "gold.sales.orders",
        "name": "Orders",
        "exposes": [
            {
                "exposeId": "orders",
                "binding": {
                    "platform": "aws",
                    "format": "parquet",
                    "location": {
                        "database": "sales",
                        "table": "orders",
                        "bucket": "acme-sales-lake",
                        "path": "gold/orders/",
                    },
                    "governance": {"lakeFormation": {"tags": dict(tags)}},
                },
                "contract": {"schema": [{"name": "order_id", "type": "string"}]},
            }
        ],
    }
    if definitions is not None:
        contract["governance"] = {"lakeFormation": {"tagDefinitions": copy.deepcopy(definitions)}}
    return contract


def _declared(document: Mapping[str, Any]) -> set:
    """Every address a ``depends_on`` may name: managed resources and data sources."""
    out = set()
    for rtype, bodies in (document.get("resource") or {}).items():
        out.update(f"{rtype}.{name}" for name in bodies)
    for rtype, bodies in (document.get("data") or {}).items():
        out.update(f"data.{rtype}.{name}" for name in bodies)
    return out


def _depends_on(document: Mapping[str, Any]) -> Iterator[tuple]:
    for rtype, bodies in (document.get("resource") or {}).items():
        for name, body in bodies.items():
            for dep in (body or {}).get("depends_on") or []:
                yield f"{rtype}.{name}", dep


def _tag_references(document: Mapping[str, Any]) -> Iterator[tuple]:
    """Each association ``lf_tag`` key that is a reference: ``(owner, tag address)``."""
    bodies = (document.get("resource") or {}).get("aws_lakeformation_resource_lf_tags") or {}
    for name, body in bodies.items():
        for tag in body.get("lf_tag") or []:
            match = TAG_REF.match(tag["key"])
            if match:
                yield f"aws_lakeformation_resource_lf_tags.{name}", match.group(1)


def _dangling(contract: Mapping[str, Any]) -> List[str]:
    """Every ``depends_on`` and every tag-key reference that names no declared resource."""
    document = json.loads(build_module(AwsIacPlugin(), contract))
    declared = _declared(document)
    return [
        f"{owner} depends_on {dep}" for owner, dep in _depends_on(document) if dep not in declared
    ] + [
        f"{owner} references {ref}"
        for owner, ref in _tag_references(document)
        if ref not in declared
    ]


def _resources(contract: Mapping[str, Any]) -> Dict[str, Any]:
    return json.loads(build_module(AwsIacPlugin(), contract))["resource"]


def _association(contract: Mapping[str, Any]) -> Dict[str, Any]:
    return _resources(contract)["aws_lakeformation_resource_lf_tags"][ASSOC]


def _refusal(contract: Mapping[str, Any]) -> UnsupportedBindingError:
    with pytest.raises(UnsupportedBindingError) as raised:
        AwsIacPlugin().emit(contract)
    return raised.value


def _validate_reports(contract: Mapping[str, Any], error: UnsupportedBindingError) -> None:
    """``fluid validate`` reports the emit's refusal, with its message and remediation."""
    errors, _ = validate_governance(contract)
    assert errors == [f"{error} {' '.join(error.remediation)}"], errors


# ---------------------------------------------------------------------------
# A key the contract does not define: a platform-owned tag
# ---------------------------------------------------------------------------


def test_a_tag_association_without_a_definition_names_no_missing_resource():
    """The reported defect: tags: {classification: confidential}, no tagDefinitions."""
    assert _dangling(_contract({"classification": "confidential"})) == []


def test_a_platform_owned_tag_is_associated_as_written_with_no_edge():
    resources = _resources(_contract({"classification": "confidential"}))
    assert "aws_lakeformation_lf_tag" not in resources
    assoc = resources["aws_lakeformation_resource_lf_tags"][ASSOC]
    assert assoc["lf_tag"] == [{"key": "classification", "value": "confidential"}]
    # Omitted, not an empty list.
    assert "depends_on" not in assoc


def test_a_platform_owned_tag_validates_with_no_error_and_no_warning():
    """No warning either: the generated pipeline's stage 2 runs ``fluid validate --strict``."""
    assert validate_governance(_contract({"classification": "confidential"})) == ([], [])


def test_only_the_keys_this_module_defines_are_references():
    """One key defined by the contract, one owned by the platform."""
    contract = _contract(
        {"classification": "confidential", "domain": "sales"},
        {"domain": ["sales", "marketing"]},
    )
    assert _dangling(contract) == []
    assoc = _association(contract)
    # The defined key names its tag, so the association waits for it; the
    # platform's key is the literal the contract wrote.
    assert assoc["lf_tag"] == [
        {"key": "classification", "value": "confidential"},
        {"key": f"${{aws_lakeformation_lf_tag.{CID}_lf_tag_domain.key}}", "value": "sales"},
    ]
    assert "depends_on" not in assoc


@pytest.mark.parametrize(
    "tags, definitions",
    [
        ({"classification": "pii_low"}, {"classification": ["pii_low"]}),
        ({"classification": "confidential", "domain": "sales"}, {"domain": ["sales"]}),
        ({"classification": "confidential"}, None),
        ({"a-b": "x"}, {"a_b": ["y"]}),
    ],
    ids=["defined", "mixed", "platform-owned", "resource-name-collision"],
)
def test_an_association_never_carries_depends_on(tags, definitions):
    """OpenTofu prefers a reference to ``depends_on``; the ordering comes from ``key``."""
    document = json.loads(build_module(AwsIacPlugin(), _contract(tags, definitions)))
    assert list(_depends_on(document)) == []
    defined = set(definitions or {})
    referenced = {ref for _, ref in _tag_references(document)}
    assert referenced == {
        f"aws_lakeformation_lf_tag.{CID}_lf_tag_{key}" for key in defined if key in tags
    }


def test_a_defined_tag_still_precedes_its_association_control():
    """Control, green on main: with the definition present the module is consistent."""
    contract = _contract({"classification": "confidential"}, {"classification": ["confidential"]})
    assert _dangling(contract) == []


def test_a_contract_that_defines_its_tags_names_each_tag_by_reference():
    """The association body of a contract that defines every key.

    ``main`` (63f3b40) emitted the literal keys and ``depends_on`` on both tags. The
    references evaluate to the same keys, so an association deployed from that module
    plans no change (``test_iac_lakeformation_tags_moto.py`` applies one and plans this).
    """
    contract = _contract(
        {"classification": "pii_low", "domain": "sales"},
        {"classification": ["public", "pii_low", "pii_high"], "domain": ["sales", "marketing"]},
    )
    resources = _resources(contract)
    table = f"aws_glue_catalog_table.{CID}_sales_orders"
    assert resources["aws_lakeformation_lf_tag"] == {
        f"{CID}_lf_tag_classification": {
            "key": "classification",
            "values": ["public", "pii_low", "pii_high"],
        },
        f"{CID}_lf_tag_domain": {"key": "domain", "values": ["sales", "marketing"]},
    }
    assert resources["aws_lakeformation_resource_lf_tags"] == {
        ASSOC: {
            "lf_tag": [
                {
                    "key": f"${{aws_lakeformation_lf_tag.{CID}_lf_tag_classification.key}}",
                    "value": "pii_low",
                },
                {"key": f"${{aws_lakeformation_lf_tag.{CID}_lf_tag_domain.key}}", "value": "sales"},
            ],
            "table": [
                {
                    "database_name": f"${{{table}.database_name}}",
                    "name": f"${{{table}.name}}",
                }
            ],
        }
    }


def test_a_key_with_an_empty_value_adds_no_reference():
    """The references come from the associations that carry a value, not from every key."""
    contract = _contract(
        {"classification": "", "domain": "sales"},
        {"classification": ["public"], "domain": ["sales"]},
    )
    assoc = _association(contract)
    assert assoc["lf_tag"] == [
        {"key": f"${{aws_lakeformation_lf_tag.{CID}_lf_tag_domain.key}}", "value": "sales"}
    ]
    assert "depends_on" not in assoc


def test_a_key_that_shares_a_defined_tags_resource_name_is_not_ordered_on_it():
    """``a-b`` and ``a_b`` are one resource name but two Lake Formation tags."""
    contract = _contract({"a-b": "x"}, {"a_b": ["y"]})
    resources = _resources(contract)
    assert resources["aws_lakeformation_lf_tag"] == {
        f"{CID}_lf_tag_a_b": {"key": "a_b", "values": ["y"]}
    }
    assoc = resources["aws_lakeformation_resource_lf_tags"][ASSOC]
    assert assoc["lf_tag"] == [{"key": "a-b", "value": "x"}]
    assert "depends_on" not in assoc
    assert validate_governance(contract) == ([], [])


def test_a_platform_owned_key_is_escaped_as_contract_text():
    """The literal key is a plain string, so the renderer escapes an interpolation in it."""
    text = build_module(AwsIacPlugin(), _contract({"${var.x}": "y"}))
    assoc = json.loads(text)["resource"]["aws_lakeformation_resource_lf_tags"][ASSOC]
    assert assoc["lf_tag"] == [{"key": "$${var.x}", "value": "y"}]


# ---------------------------------------------------------------------------
# Refused, at emit and at ``fluid validate``
# ---------------------------------------------------------------------------


def test_a_value_the_definition_does_not_allow_is_refused():
    contract = _contract({"classification": "confidential"}, {"classification": ["public"]})
    error = _refusal(contract)
    assert error.kind == "lakeformation-tag-value"
    assert "exposes[orders] governance.lakeFormation.tags.classification" in str(error)
    assert "'confidential'" in str(error) and "['public']" in str(error)
    _validate_reports(contract, error)


@pytest.mark.parametrize("value", ["public", "Public", "PUBLIC"])
def test_a_value_is_compared_case_folded(value):
    """Lake Formation stores keys and values lower-cased."""
    contract = _contract({"classification": value}, {"classification": ["Public", "internal"]})
    key = f"${{aws_lakeformation_lf_tag.{CID}_lf_tag_classification.key}}"
    assert _association(contract)["lf_tag"] == [{"key": key, "value": value}]
    assert validate_governance(contract) == ([], [])


@pytest.mark.parametrize("values", [[], None])
def test_a_definition_with_no_values_is_refused(values):
    """It used to be skipped, and an association to it named an undeclared resource."""
    for contract in (
        _contract({"classification": "public"}, {"classification": values}),
        _contract({}, {"classification": values}),
    ):
        error = _refusal(contract)
        assert error.kind == "lakeformation-tag-definition"
        assert "tagDefinitions.classification lists no values" in str(error)
        _validate_reports(contract, error)


def test_two_definitions_that_are_one_resource_name_are_refused():
    """``a-b`` and ``a_b`` were both ``..._lf_tag_a_b``, and the later replaced the earlier."""
    contract = _contract({}, {"a-b": ["x"], "a_b": ["y"]})
    error = _refusal(contract)
    assert error.kind == "lakeformation-tag-definition"
    assert "'a-b' and 'a_b'" in str(error)
    assert f"aws_lakeformation_lf_tag.{CID}_lf_tag_a_b" in str(error)
    _validate_reports(contract, error)


@pytest.mark.parametrize("missing", ["table", "database"])
def test_tags_on_a_binding_with_no_glue_table_are_refused(missing):
    """They used to be dropped without a word."""
    contract = _contract({"classification": "confidential"})
    del contract["exposes"][0]["binding"]["location"][missing]
    error = _refusal(contract)
    assert error.kind == "lakeformation-tag-association"
    assert "associates classification" in str(error)
    assert "names no Glue table" in str(error)
    _validate_reports(contract, error)


def test_tags_with_no_value_on_a_binding_with_no_table_are_not_refused():
    contract = _contract({"classification": ""})
    del contract["exposes"][0]["binding"]["location"]["table"]
    assert "aws_lakeformation_resource_lf_tags" not in AwsIacPlugin().emit(contract)
    assert validate_governance(contract) == ([], [])


def test_tags_on_a_format_lake_formation_does_not_govern_are_ignored_as_before():
    """The whole governance.lakeFormation block of a non-Glue binding is ignored, by design."""
    contract = _contract({"classification": "confidential"})
    binding = contract["exposes"][0]["binding"]
    binding["format"] = "kinesis_stream"
    binding["location"] = {"stream": "orders"}
    assert "aws_lakeformation_resource_lf_tags" not in AwsIacPlugin().emit(contract)
    assert validate_governance(contract) == ([], [])


def test_a_gcp_only_contract_is_not_checked_for_lake_formation_tags():
    """Only the AWS emitter creates the tags; a contract it never runs on is not refused."""
    contract = _contract({}, {"classification": []})
    contract["exposes"][0]["binding"] = {
        "platform": "gcp",
        "format": "bigquery_table",
        "location": {"project": "p", "dataset": "d", "table": "orders"},
    }
    errors, _ = validate_governance(contract)
    assert not [e for e in errors if "tagDefinitions" in e], errors


# ---------------------------------------------------------------------------
# The schemas say so
# ---------------------------------------------------------------------------


def _lake_formation_descriptions():
    for path in sorted(SCHEMAS.glob("fluid-schema-*.json")):
        defs = json.loads(path.read_text(encoding="utf-8")).get("$defs") or {}
        top = ((defs.get("governance") or {}).get("properties") or {}).get("lakeFormation")
        per_binding = ((defs.get("bindingGovernance") or {}).get("properties") or {}).get(
            "lakeFormation"
        )
        if top or per_binding:
            yield (
                path.name,
                top["properties"]["tagDefinitions"]["description"],
                per_binding["properties"]["tags"]["description"],
            )


def test_every_schema_that_carries_the_tags_is_covered():
    assert [name for name, _, _ in _lake_formation_descriptions()] == [
        "fluid-schema-0.7.3.json",
        "fluid-schema-0.7.4.json",
        "fluid-schema-0.7.5.json",
        "fluid-schema-0.7.6.json",
    ]


@pytest.mark.parametrize(
    "name,definitions,tags",
    list(_lake_formation_descriptions()),
    ids=[name for name, _, _ in _lake_formation_descriptions()],
)
def test_every_schema_says_a_key_may_be_owned_outside_the_contract(name, definitions, tags):
    # The old claim, which nothing enforced and the emitter assumed.
    assert "Keys must reference tag definitions" not in tags
    # A key the contract does not define: what the operator must have in place.
    assert "must already exist in the account" in tags
    assert "needs the Lake Formation ASSOCIATE permission on it" in tags
    # A key the contract defines is created by the apply, so in one place only.
    assert "must not also be created elsewhere" in tags
    assert "must not also be created elsewhere" in definitions
    assert "The apply CREATES every tag defined here" in definitions
    # The refusals.
    assert "must be one of the tag's values" in tags
    assert "names no Glue table" in tags
    assert "A key with no values is refused" in definitions
    assert "one resource name, such as a-b and a_b" in definitions


# ---------------------------------------------------------------------------
# ``tofu validate`` (needs tofu on PATH; skips without it)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tags, definitions",
    [
        ({"classification": "confidential"}, None),
        ({"classification": "confidential", "domain": "sales"}, {"domain": ["sales"]}),
    ],
    ids=["platform-owned", "platform-owned-beside-a-defined-tag"],
)
def test_the_module_validates_under_tofu(tofu_binary, tofu_env, tmp_path, tags, definitions):
    """``tofu init -backend=false`` + ``tofu validate`` on the reported contract."""
    text = build_module(AwsIacPlugin(), _contract(tags, definitions))
    (tmp_path / "main.tf.json").write_text(text, encoding="utf-8")
    init = runner.tofu_init(str(tmp_path), backend=False, env=tofu_env)
    assert init.ok, f"tofu init failed:\n{init.stderr or init.stdout}"
    result = runner.tofu_validate(str(tmp_path), env=tofu_env)
    assert result.ok, f"tofu validate failed:\n{result.stderr or result.stdout}"
