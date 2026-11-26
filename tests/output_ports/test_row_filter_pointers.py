# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A pointer in an expose's columns does not make the expose filtered.

``declares_row_filters`` decides from the expose's policy alone: a ``$ref`` left in its
contract, schema or a column is not read as a row filter. With ``contract=`` (the whole
document as written) the same-document pointers are resolved first, so a policy kept
behind ``#/x-shared/...`` is read; a pointer left in the expose or its policy (another
file, say) is still refused. The read paths (``row_filter_predicate``,
``compile_semantic_query``, ``compile_free_form_sql``) apply column sets their caller
computed from the expose as passed, so they refuse any unresolved pointer, with or
without ``contract=``.
"""

from __future__ import annotations

import copy
from typing import Any, Dict

import pytest

from fluid_build.output_ports.mcp import query_compiler as qc

ANALYST = "group:analysts@library.test"
BRANCH = "group:branch-north@library.test"

_COLUMNS = [
    {"name": "id", "type": "integer"},
    {"name": "branch", "type": "string"},
    {"name": "email", "type": "string", "sensitivity": "pii"},
    {"name": "amount", "type": "integer"},
]
_POLICY = {"authz": {"rowFilters": [{"principal": BRANCH, "where": "branch = 'north'"}]}}


def _document(expose: Dict[str, Any], **shared: Any) -> Dict[str, Any]:
    return {
        "exposes": [expose],
        "x-shared": {"cols": copy.deepcopy(_COLUMNS), "policy": copy.deepcopy(_POLICY), **shared},
    }


def _expose(**parts: Any) -> Dict[str, Any]:
    expose: Dict[str, Any] = {
        "exposeId": "orders",
        "contract": {"schema": copy.deepcopy(_COLUMNS)},
        "semantics": {"measures": [{"name": "total", "agg": "sum", "expr": "amount"}]},
    }
    expose.update(parts)
    return expose


UNFILTERED = {
    "schema-pointer": {"contract": {"schema": {"$ref": "#/x-shared/cols"}}},
    "column-pointer": {
        "contract": {"schema": [{"$ref": "#/x-shared/cols/0"}, *copy.deepcopy(_COLUMNS[1:])]}
    },
    "contract-pointer": {"contract": {"$ref": "#/x-shared/contract"}},
}


@pytest.mark.parametrize("name", sorted(UNFILTERED))
def test_a_pointer_in_the_columns_does_not_make_an_unfiltered_expose_filtered(name):
    expose = _expose(**UNFILTERED[name])
    document = _document(expose, contract={"schema": copy.deepcopy(_COLUMNS)})
    assert qc.declares_row_filters(expose) is False
    assert qc.declares_row_filters(expose, contract=document) is False


@pytest.mark.parametrize("where", ["policy", "authz"])
def test_a_policy_behind_a_same_document_pointer_is_read_with_the_contract(where):
    if where == "policy":
        expose = _expose(policy={"$ref": "#/x-shared/policy"})
    else:
        expose = _expose(policy={"authz": {"$ref": "#/x-shared/policy/authz"}})
    document = _document(expose)
    assert qc.declares_row_filters(expose, contract=document) is True
    with pytest.raises(qc.RowFilterUnavailable):
        qc.declares_row_filters(expose)


@pytest.mark.parametrize(
    "policy", [{"$ref": "other.yaml#/policy"}, {"authz": {"$ref": "other.yaml#/authz"}}]
)
def test_a_pointer_to_another_file_in_the_policy_is_refused_either_way(policy):
    expose = _expose(policy=policy)
    document = _document(expose)
    with pytest.raises(qc.RowFilterUnavailable):
        qc.declares_row_filters(expose, contract=document)
    with pytest.raises(qc.RowFilterUnavailable):
        qc.declares_row_filters(expose)


def test_an_expose_that_is_itself_a_pointer_is_refused():
    expose = {"$ref": "#/x-shared/expose"}
    with pytest.raises(qc.RowFilterUnavailable):
        qc.declares_row_filters(expose)


def test_the_expose_is_found_by_equality_when_it_is_not_the_documents_own_object():
    expose = _expose(policy={"$ref": "#/x-shared/policy"})
    document = _document(copy.deepcopy(expose))
    assert qc.declares_row_filters(expose, contract=document) is True


def test_a_document_that_cannot_be_resolved_leaves_the_expose_as_written():
    expose = _expose(policy={"$ref": "#/x-shared/missing"})
    document = _document(expose)
    with pytest.raises(qc.RowFilterUnavailable):
        qc.declares_row_filters(expose, contract=document)


# -- the read paths refuse a pointer, with or without contract= ---------------------------
#
# A read applies the column restrictions and sensitivity flags its caller computed from the
# expose as passed. A pointer the read resolved for itself would leave those behind, so
# every read path refuses an unresolved pointer and wants the document resolved first.

POINTER_READS = {
    "policy": {"policy": {"$ref": "#/x-shared/policy"}},
    "columns": {
        "policy": copy.deepcopy(_POLICY),
        "contract": {"schema": {"$ref": "#/x-shared/cols"}},
    },
}


@pytest.mark.parametrize("name", sorted(POINTER_READS))
def test_row_filter_predicate_refuses_a_pointer_even_with_the_contract(name):
    expose = _expose(**copy.deepcopy(POINTER_READS[name]))
    document = _document(expose)
    for contract in (document, None):
        with pytest.raises(qc.RowFilterUnavailable, match="unresolved"):
            qc.row_filter_predicate(expose, {"principal": BRANCH}, contract=contract)


def test_a_policy_behind_a_pointer_is_never_served_without_its_column_restrictions():
    # The caller computed no restricted column from the expose as passed, where the policy
    # is only a pointer; the read is refused rather than served without the restriction.
    shared = copy.deepcopy(_POLICY)
    shared["authz"]["columnRestrictions"] = [{"columns": ["email"], "access": "deny"}]
    expose = _expose(policy={"$ref": "#/x-shared/policy"})
    document = _document(expose, policy=shared)
    with pytest.raises(qc.RowFilterUnavailable):
        qc.compile_free_form_sql(
            sql="SELECT email, amount FROM orders",
            table_reference="orders",
            limit=10,
            expose=expose,
            caller_attributes={"principal": BRANCH},
            dialect="duckdb",
            restricted_columns=(),
            contract=document,
        )
    with pytest.raises(qc.RowFilterUnavailable):
        qc.compile_semantic_query(
            expose=expose,
            measure="total",
            dimensions=["email"],
            limit=10,
            table_reference="orders",
            dialect="duckdb",
            caller_attributes={"principal": BRANCH},
            contract=document,
        )


def test_compile_semantic_query_refuses_columns_behind_a_pointer_even_with_the_contract():
    # The sensitivity flag on ``email`` sits behind the pointer, where the caller's
    # redacted columns cannot see it.
    expose = _expose(contract={"schema": {"$ref": "#/x-shared/cols"}})
    document = _document(expose)
    for contract in (document, None):
        with pytest.raises(qc.RowFilterUnavailable):
            qc.compile_semantic_query(
                expose=expose,
                measure="total",
                dimensions=["email"],
                limit=10,
                table_reference="orders",
                dialect="duckdb",
                contract=contract,
            )


@pytest.mark.parametrize(
    "access",
    [{"$ref": "#/x-shared/access"}, {"grants": [{"$ref": "#/x-shared/access/grants/0"}]}],
    ids=["policy", "grant"],
)
def test_an_access_policy_behind_a_pointer_is_refused(access):
    expose = _expose(
        policy={"authz": {"readers": [], "rowFilters": [{"principal": BRANCH, "where": "id = 1"}]}}
    )
    document = _document(expose)
    document["x-shared"]["access"] = {"grants": [{"principal": ANALYST, "permissions": ["read"]}]}
    document["accessPolicy"] = access
    with pytest.raises(qc.RowFilterUnavailable):
        qc.row_filter_predicate(expose, {"principal": ANALYST}, contract=document)
