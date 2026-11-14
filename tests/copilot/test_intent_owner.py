# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A contract forged from an intent keeps the owner the intent names.

``data_product.owner`` was dropped: every from-intent contract carried
``metadata.owner.team: data-team``, the template default ``fluid validate``
flags as residue, so each Command Center draft came back with a warning.
The owner now comes from the intent, and only from it: a provider's
``source_summary`` cannot replace it or invent one.
"""

from __future__ import annotations

import pytest

from fluid_build.copilot.agents.base import StageSession
from fluid_build.copilot.agents.contract_forge_agent import ContractForgeAgent
from fluid_build.copilot.agents.modeler_agent import ModelerAgent
from fluid_build.copilot.schemas.intent import BusinessIntent
from fluid_build.copilot.store.backends.null import NullBackend
from fluid_build.forge_datamodel.from_intent.intent_loader import INTENT_EXAMPLES

pytestmark = pytest.mark.unit


def _retail_intent(*, owner: str | None = None) -> BusinessIntent:
    """The shipped ``--example retail`` intent, whose owner is ``retail-analytics``."""
    intent = BusinessIntent.model_validate(INTENT_EXAMPLES["retail"])
    if owner is None:
        return intent
    return intent.model_copy(
        update={"data_product": intent.data_product.model_copy(update={"owner": owner})}
    )


def _owner(logical) -> tuple:
    contract = ContractForgeAgent().forge_contract(
        StageSession(store=NullBackend()), logical=logical
    )
    return contract["metadata"]["owner"]["team"], contract["labels"].get("catalogOwnerSource")


@pytest.mark.parametrize("technique", ["dimensional", "data_vault_2", "flat"])
def test_the_intent_owner_becomes_the_contract_owner(technique: str) -> None:
    draft = ModelerAgent().from_intent(
        StageSession(store=NullBackend()), intent=_retail_intent(), technique=technique
    )

    assert _owner(draft) == ("retail-analytics", "intent")


def test_without_an_owner_the_default_stays() -> None:
    draft = ModelerAgent().from_intent(
        StageSession(store=NullBackend()), intent=_retail_intent(owner=""), technique="dimensional"
    )

    assert "dominant_owner" not in draft.source_summary
    assert _owner(draft) == ("data-team", None)


@pytest.mark.parametrize(
    ("intent_owner", "expected"),
    [(None, ("retail-analytics", "intent")), ("", ("data-team", None))],
)
def test_a_provider_cannot_replace_or_invent_the_owner(intent_owner, expected) -> None:
    modeler = ModelerAgent()
    intent = _retail_intent(owner=intent_owner)
    provider_result = modeler.from_intent(
        StageSession(store=NullBackend()), intent=intent, technique="dimensional"
    )
    provider_result.source_summary.update(
        {"dominant_owner": "guessed-team", "dominant_owner_source": "tag"}
    )

    anchored = modeler._deterministic_intent_backbone(
        provider_result=provider_result, intent=intent, technique="dimensional"
    )

    assert _owner(anchored) == expected
