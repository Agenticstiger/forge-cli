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

"""Coverage for the DV2 table-name conventions.

Closes the second half of plan-gap A2 — until now ``naming.py`` was
shipped without a single direct test, so any change to slug rules
(non-alphanumeric collapse, leading/trailing trim, case folding) or
prefix idempotency could silently rename every emitted table and
break everyone's downstream dbt refs.

The conventions pinned below are:

* ``hub_<entity>``            — one row per business key
* ``lnk_<e1>_<e2>_…``         — relationships, order is semantic
* ``sat_<parent>[_<descr>]``  — descriptive context against a hub
* ``pit_<parent>``            — point-in-time helper against a hub
* ``br_<descriptor>``         — bridge against a relationship

All five helpers must be **idempotent**: passing an already-prefixed
name in must yield it back unchanged. This is the contract that lets
the modeler emit "the hub for ``hub_party``" without needing to know
whether ``hub_party`` is already a fully-qualified name.
"""

from __future__ import annotations

from fluid_build.forge_datamodel.dv2.naming import (
    bridge_name,
    hub_name,
    iter_standard_prefixes,
    link_name,
    pit_name,
    satellite_name,
)

# ----------------------------------------------------------------------
# Slug rules — pin the non-alphanumeric collapse + case folding
# ----------------------------------------------------------------------


class TestSlugRules:
    def test_hub_name_lower_cases_and_collapses_non_alnum(self):
        assert hub_name("Customer Orders (V2)") == "hub_customer_orders_v2"

    def test_hub_name_strips_leading_and_trailing_underscores(self):
        """Adjacent non-alphanumerics must collapse and any trailing
        underscores must be trimmed so we never emit ``hub_party_``."""
        assert hub_name("--Party--") == "hub_party"

    def test_hub_name_with_unicode_falls_back_to_underscore(self):
        """Non-ASCII letters are not in the safe set — they collapse to
        ``_``. Keeps emitted names ASCII-only, which is what every
        downstream warehouse expects."""
        assert hub_name("café") == "hub_caf"

    def test_hub_name_empty_returns_bare_prefix(self):
        """An empty input must not produce ``hub__`` or ``hub_`` — the
        helper falls back to the bare prefix so the caller sees an
        obviously broken name and fixes the input."""
        assert hub_name("") == "hub"
        assert hub_name("   ") == "hub"


# ----------------------------------------------------------------------
# Idempotence — passing a prefixed name in yields it back unchanged
# ----------------------------------------------------------------------


class TestPrefixIdempotence:
    def test_hub_name_idempotent(self):
        assert hub_name("hub_party") == "hub_party"

    def test_satellite_name_strips_hub_prefix_before_relabelling(self):
        """``satellite_name("hub_party")`` should yield ``sat_party``,
        not ``sat_hub_party`` — the parent-name is logically the
        entity, not the hub-table-name."""
        assert satellite_name("hub_party") == "sat_party"

    def test_pit_name_strips_hub_prefix(self):
        assert pit_name("hub_customer") == "pit_customer"

    def test_satellite_name_idempotent(self):
        assert satellite_name("sat_customer_profile") == "sat_customer_profile"

    def test_pit_name_idempotent(self):
        assert pit_name("pit_customer") == "pit_customer"

    def test_link_name_idempotent_when_already_prefixed(self):
        assert link_name("lnk_order_customer") == "lnk_order_customer"

    def test_bridge_name_idempotent(self):
        assert bridge_name("br_order_promo") == "br_order_promo"


# ----------------------------------------------------------------------
# link_name — multi-entity ordering is semantic
# ----------------------------------------------------------------------


class TestLinkName:
    def test_two_entity_link(self):
        assert link_name("order", "customer") == "lnk_order_customer"

    def test_three_entity_link(self):
        assert link_name("order", "customer", "promotion") == "lnk_order_customer_promotion"

    def test_link_order_is_semantic(self):
        """Order matters — ``lnk_order_customer`` and
        ``lnk_customer_order`` are different relationships, and the
        helper must preserve the caller-supplied direction."""
        a = link_name("order", "customer")
        b = link_name("customer", "order")
        assert a != b

    def test_empty_components_dropped(self):
        """Falsy entries must not produce double underscores or trailing
        ``_``; they are simply skipped."""
        assert link_name("order", "", "customer") == "lnk_order_customer"

    def test_zero_entities_returns_bare_prefix(self):
        assert link_name() == "lnk"


# ----------------------------------------------------------------------
# satellite_name — descriptor handling
# ----------------------------------------------------------------------


class TestSatelliteName:
    def test_satellite_with_descriptor(self):
        assert satellite_name("hub_customer", "profile") == "sat_customer_profile"

    def test_satellite_without_descriptor(self):
        assert satellite_name("hub_customer") == "sat_customer"

    def test_satellite_descriptor_slugified(self):
        """The descriptor goes through the same slug rules as the
        parent name."""
        assert satellite_name("hub_customer", "Profile (PII)") == "sat_customer_profile_pii"

    def test_satellite_for_non_hub_parent(self):
        """Some stages compose satellites against logical entities
        rather than hub-table-names — both shapes must work."""
        assert satellite_name("party") == "sat_party"


# ----------------------------------------------------------------------
# Prefix exposure — for validators / linters
# ----------------------------------------------------------------------


class TestStandardPrefixes:
    def test_returns_the_five_canonical_prefixes_in_emit_order(self):
        """The validator agent and CI linter both rely on this tuple
        to whitelist DV2-shaped table names. The exact set and order
        is the public contract."""
        assert tuple(iter_standard_prefixes()) == ("hub", "lnk", "sat", "pit", "br")

    def test_every_helper_emits_one_of_the_standard_prefixes(self):
        """Sanity round-trip: ``iter_standard_prefixes`` must enumerate
        every prefix actually used by the helpers."""
        emitted = {
            hub_name("e").split("_", 1)[0],
            link_name("a", "b").split("_", 1)[0],
            satellite_name("hub_e").split("_", 1)[0],
            pit_name("hub_e").split("_", 1)[0],
            bridge_name("d").split("_", 1)[0],
        }
        assert emitted == set(iter_standard_prefixes())
