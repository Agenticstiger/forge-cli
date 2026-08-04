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

"""Pin the AWS sovereignty util against the ``dataResidency`` shape confusion.

``sovereignty.dataResidency`` is typed ``boolean`` in every bundled schema
(0.7.1 through 0.7.6) — "must this data stay inside the declared
jurisdiction?" — and the region allow-list lives in the sibling
``allowedRegions``. This module read the allow-list *out of* ``dataResidency``,
so the documented, schema-default, example-endorsed ``true`` raised
``TypeError: argument of type 'bool' is not a container or iterable`` while
``false`` quietly disabled the check.

That asymmetry is the point of these tests: the *strict* setting was the one
that broke and the *permissive* one was the one that worked, which is exactly
backwards for a governance control.
"""

import logging

import pytest

from fluid_build._errors import ResidencyViolationError
from fluid_build.cli._errors import SovereigntyViolationError
from fluid_build.providers.aws.util.sovereignty import SovereigntyValidator

# The two veto types are SIBLINGS — each derives straight from
# ``FluidUserError`` — so a test that expects only one silently passes on the
# wrong outcome. Assert against both wherever either is a legal answer.
VETOES = (SovereigntyViolationError, ResidencyViolationError)

pytestmark = pytest.mark.unit


def _binding(region):
    return {"location": {"region": region}}


def _contract(sovereignty):
    return {"sovereignty": sovereignty}


class TestDataResidencyIsABoolean:
    """The schema shape must work, and must work in the safe direction."""

    def test_true_with_compliant_region_passes(self):
        SovereigntyValidator().validate(
            _contract(
                {"jurisdiction": "EU", "dataResidency": True, "allowedRegions": ["eu-west-1"]}
            ),
            _binding("eu-west-1"),
        )

    def test_true_with_region_outside_the_allow_list_is_refused(self):
        """Previously a TypeError, i.e. the check never ran at all."""
        with pytest.raises(VETOES):
            SovereigntyValidator().validate(
                _contract({"dataResidency": True, "allowedRegions": ["eu-west-1"]}),
                _binding("us-east-1"),
            )

    def test_false_does_not_disarm_the_allow_list(self):
        """``dataResidency: false`` is not "delete my allowedRegions".

        The two keys are orthogonal in the schema, and the canonical engine
        (``policy/sovereignty.py``) enforces ``allowedRegions`` ungated. Gating
        it here would make the AWS provider quietly more permissive than the
        ``fluid validate`` stage that runs before it.
        """
        with pytest.raises(VETOES):
            SovereigntyValidator().validate(
                _contract({"dataResidency": False, "allowedRegions": ["eu-west-1"]}),
                _binding("us-east-1"),
            )

    def test_absent_key_does_not_disarm_the_allow_list(self):
        """An omitted ``dataResidency`` is falsy in Python but ``default: true``
        in the schema — gating on it would invert the declared default."""
        with pytest.raises(VETOES):
            SovereigntyValidator().validate(
                _contract({"allowedRegions": ["eu-west-1"]}), _binding("eu-south-1")
            )

    def test_false_without_an_allow_list_is_a_genuine_opt_out(self):
        SovereigntyValidator().validate(_contract({"dataResidency": False}), _binding("us-east-1"))

    def test_true_without_an_allow_list_defers_to_the_jurisdiction_check(self):
        """Residency required but no regions named — nothing to compare against.

        The jurisdiction check is the binding constraint in that case; this
        must not invent a violation, and must not raise.
        """
        SovereigntyValidator().validate(_contract({"dataResidency": True}), _binding("us-east-1"))


class TestDeniedRegions:
    """``deniedRegions`` is a real schema key the AWS util never consulted."""

    def test_denied_region_is_refused_even_when_residency_is_off(self):
        with pytest.raises(VETOES):
            SovereigntyValidator().validate(
                _contract({"dataResidency": False, "deniedRegions": ["us-east-1"]}),
                _binding("us-east-1"),
            )

    def test_undenied_region_passes(self):
        SovereigntyValidator().validate(
            _contract({"dataResidency": False, "deniedRegions": ["us-east-1"]}),
            _binding("eu-west-1"),
        )


class TestNonSchemaShapesDoNotCrash:
    """Shapes that exist in the wild must degrade, never raise ``TypeError``.

    A list was this module's own docstring example and one stale fixture; a
    dict is what ``cli/init_scan.py`` emits. Neither is schema-valid, but a
    governance control must not fall over on them — and, for the list, must not
    silently drop a stated constraint either.
    """

    def test_legacy_list_is_honoured_with_a_warning(self, caplog):
        with caplog.at_level(logging.WARNING):
            with pytest.raises(VETOES):
                SovereigntyValidator().validate(
                    _contract({"dataResidency": ["eu-west-1"]}), _binding("us-east-1")
                )
        assert "allowedRegions" in caplog.text

    def test_legacy_list_still_admits_a_listed_region(self):
        SovereigntyValidator().validate(
            _contract({"dataResidency": ["eu-west-1"]}), _binding("eu-west-1")
        )

    def test_legacy_list_never_widens_an_explicit_allow_list(self):
        """Precedence, not union.

        ``allowedRegions`` is what a reviewer, an OPA gate or a marketplace
        facet reads. If a schema-invalid ``dataResidency`` list were merged in,
        it could only ever ADD regions the valid allow-list deliberately
        excluded — widening the policy invisibly to every one of those readers.
        """
        with pytest.raises(VETOES):
            SovereigntyValidator().validate(
                _contract({"allowedRegions": ["eu-west-1"], "dataResidency": ["eu-south-1"]}),
                _binding("eu-south-1"),
            )

    def test_object_shape_allow_list_is_unwrapped_not_key_matched(self):
        """``region not in {"allowedRegions": [...]}`` tested the dict's KEYS."""
        with pytest.raises(VETOES):
            SovereigntyValidator().validate(
                _contract({"dataResidency": {"allowedRegions": ["eu-west-1"]}}),
                _binding("us-east-1"),
            )

    def test_object_shape_does_not_raise_typeerror(self):
        """``{"allowedRegions": [...]}`` used to make ``region not in <dict>``
        test the dict's KEYS, refusing a perfectly legal region."""
        SovereigntyValidator().validate(
            _contract({"dataResidency": {"allowedRegions": ["eu-west-1"]}}),
            _binding("eu-west-1"),
        )


class TestExtractTags:
    """``extract_tags`` joined the value directly — ``",".join(True)``."""

    def test_true_emits_enforced_plus_the_allow_list(self):
        tags = SovereigntyValidator().extract_tags(
            _contract(
                {"jurisdiction": "EU", "dataResidency": True, "allowedRegions": ["eu-west-1"]}
            )
        )
        assert tags["fluid:data_residency"] == "enforced"
        assert tags["fluid:allowed_regions"] == "eu-west-1"
        assert tags["fluid:data_jurisdiction"] == "EU"

    def test_false_emits_no_residency_tag(self):
        tags = SovereigntyValidator().extract_tags(
            _contract({"jurisdiction": "EU", "dataResidency": False})
        )
        assert "fluid:data_residency" not in tags

    def test_true_without_regions_emits_no_empty_allow_list_tag(self):
        tags = SovereigntyValidator().extract_tags(_contract({"dataResidency": True}))
        assert tags["fluid:data_residency"] == "enforced"
        assert "fluid:allowed_regions" not in tags

    @pytest.mark.parametrize(
        "shape", [True, False, ["eu-west-1"], {"allowedRegions": ["eu-west-1"]}, None]
    )
    def test_no_shape_raises(self, shape):
        """The join used to emit the dict's KEYS as a cloud tag value —
        ``fluid:allowed_regions = "allowedRegions"``."""
        tags = SovereigntyValidator().extract_tags(_contract({"dataResidency": shape}))
        assert tags.get("fluid:allowed_regions") != "allowedRegions"


class TestGenerateIacDoesNotSwallowARefusal:
    """`fluid generate iac` must not emit a module for a refused contract.

    ``cli/generate_iac.py::_native_actions`` wraps the native planner in a
    best-effort ``except Exception`` that logs at DEBUG and returns ``[]``. The
    provider's sovereignty check is the only place AWS enforces
    jurisdiction/residency, so that swallow turned a deliberate refusal into a
    silent one: `fluid generate iac` on a contract bound outside its declared
    jurisdiction logged the violation and then wrote ``main.tf.json`` anyway
    and exited 0.

    "Best-effort" has to mean a planner that could not RUN, not a planner that
    ran and said no.
    """

    @staticmethod
    def _refusal(exc_type, message="nope"):
        from fluid_build.cli.generate_iac import _is_sovereignty_refusal

        try:
            raise exc_type.for_connector(connector="aws:us-east-1", jurisdiction="EU")
        except Exception as direct:  # noqa: BLE001
            return _is_sovereignty_refusal(direct)

    def test_direct_sovereignty_error_is_recognised(self):
        assert self._refusal(SovereigntyViolationError) is True

    def test_wrapped_refusal_is_recognised_through_the_cause_chain(self):
        """Providers re-raise as ``ProviderError(...) from e``."""
        from fluid_build.cli.generate_iac import _is_sovereignty_refusal

        try:
            try:
                raise SovereigntyViolationError.for_connector(
                    connector="aws:us-east-1", jurisdiction="EU"
                )
            except SovereigntyViolationError as inner:
                raise RuntimeError("Failed to plan AWS deployment") from inner
        except RuntimeError as outer:
            assert _is_sovereignty_refusal(outer) is True

    def test_residency_sibling_is_recognised(self):
        """The sibling type is half the control; matching only one misses it."""
        from fluid_build.cli.generate_iac import _is_sovereignty_refusal

        try:
            raise ResidencyViolationError.for_transfer(
                from_region="us-east-1", to_region="<denied>", jurisdiction="EU"
            )
        except ResidencyViolationError as exc:
            assert _is_sovereignty_refusal(exc) is True

    def test_an_ordinary_planner_failure_is_still_best_effort(self):
        """A planner that could not run must stay swallowed, as before."""
        from fluid_build.cli.generate_iac import _is_sovereignty_refusal

        try:
            raise RuntimeError("boto3 not installed")
        except RuntimeError as exc:
            assert _is_sovereignty_refusal(exc) is False

    def test_a_self_referential_cause_chain_terminates(self):
        """Guard the walk against a cycle rather than hanging the CLI."""
        from fluid_build.cli.generate_iac import _is_sovereignty_refusal

        a = RuntimeError("a")
        b = RuntimeError("b")
        a.__cause__ = b
        b.__cause__ = a
        assert _is_sovereignty_refusal(a) is False
