# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""What the operator lets a forge Spark job run that the contract wrote itself.

The managed Flink lane's rule (:func:`fluid_build.streaming.policy.refuse_unless_contract_sql_allowed`),
with its own variable: a Spark job's identity reads the whole Glue catalog its role
can, so a contract's own ``properties.sql`` runs only for a build the operator lists.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from fluid_build._errors import FluidUserError, doc_url
from fluid_build.spark.compiler import CONTRACT_SQL_ALLOWLIST_ENV
from fluid_build.streaming.contract_sql import listed_sql_refusal


@dataclass
class SparkPolicyError(FluidUserError):
    """A Spark build asks for something the operator has not allowed.

    Raised while its job config is compiled, before anything is uploaded. Messages
    name builds and variables, never values.
    """

    code: str = "SparkPolicyError"


def refuse_unless_contract_sql_allowed(
    product_id: Any, build: Mapping[str, Any], environ: Optional[Mapping[str, str]] = None
) -> None:
    """Raise unless :data:`CONTRACT_SQL_ALLOWLIST_ENV` lists a build whose SQL the job runs.

    An entry is ``<product>/<build>@sha256:<digest of the stripped SQL>`` (the reviewed
    SQL only, recommended), ``<product>/<build>`` or ``<product>``. An unset list
    allows nothing.
    """
    refusal = listed_sql_refusal(CONTRACT_SQL_ALLOWLIST_ENV, product_id, build, environ)
    if refusal is None:
        return
    stale, listed = refusal
    build_id = build.get("id")
    raise SparkPolicyError(
        what=f"Spark build {build_id!r} runs SQL written in the contract",
        why=(
            (
                f"{CONTRACT_SQL_ALLOWLIST_ENV} approves other SQL for {product_id}/{build_id}: "
                "the build's SQL changed after it was listed. "
                if stale
                else f"{product_id}/{build_id} is not in {CONTRACT_SQL_ALLOWLIST_ENV}. "
            )
            + "A contract's own SQL runs inside the job with its role: it can read any table "
            "the role can read in the Glue catalog and copy it into this product's table. "
            "An unset list allows nothing."
        ),
        fix=(
            "Declare the output with properties.stream and properties.mapping, which forge "
            "compiles into a query that reads only the stream; or, once this SQL is reviewed, "
            "have the operator list it in the real environment (a project .env cannot set it), "
            f'pinned to what was reviewed: export {CONTRACT_SQL_ALLOWLIST_ENV}="{listed}".'
        ),
        doc=doc_url(),
    )
