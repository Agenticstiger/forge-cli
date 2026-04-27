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

"""Coverage for fluid_build.cli.provider_action_executor.ProviderActionExecutor.

The executor maps a parsed-contract action to a provider-specific handler.
Snowflake previously raised ``NotImplementedError`` from ``_get_handler_for_provider``
(unconditionally crashing any caller that routed through this executor); this
suite locks in the post-fix behaviour: same generic dispatch as AWS, no crash.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from fluid_build.cli.provider_action_executor import (
    AirflowActionHandler,
    GenericProviderActionHandler,
    ProviderActionExecutor,
)


@pytest.fixture
def executor():
    return ProviderActionExecutor(logger=logging.getLogger("test"))


class TestSnowflakeHandlerWiring:
    def test_snowflake_does_not_raise_not_implemented_error(self, executor):
        """The pre-fix code raised NotImplementedError from _get_handler_for_provider
        for snowflake. The fix routes Snowflake through GenericProviderActionHandler."""
        provider = MagicMock(name="snowflake_provider")
        try:
            handler = executor._get_handler_for_provider("snowflake", provider)
        except NotImplementedError as exc:
            pytest.fail(f"Snowflake handler should not raise NotImplementedError: {exc}")
        assert isinstance(handler, GenericProviderActionHandler)

    def test_snowflake_handler_is_cached_per_provider(self, executor):
        provider = MagicMock(name="snowflake_provider")
        first = executor._get_handler_for_provider("snowflake", provider)
        second = executor._get_handler_for_provider("snowflake", provider)
        assert first is second

    def test_snowflake_handler_dispatches_to_execute_method_when_present(self, executor):
        """A provider that exposes ``execute_<action_type>`` is invoked by the
        generic handler. Snowflake's enhanced provider can opt in to this path."""

        class FakeProvider:
            def execute_provisionDataset(self, params):  # noqa: N802 - matches camelCase op
                return {"status": "ok", "params": params}

        action = MagicMock(provider="snowflake", action_id="a1")
        action.action_type.value = "provisionDataset"
        action.params = {"binding": {"platform": "snowflake"}}

        handler = executor._get_handler_for_provider("snowflake", FakeProvider())
        result = handler._execute_single_action(action)

        assert result == {"status": "ok", "params": {"binding": {"platform": "snowflake"}}}

    def test_snowflake_handler_falls_back_to_not_implemented_when_method_missing(self, executor):
        """When the provider does not expose ``execute_<action_type>``, the
        generic handler returns a ``not_implemented`` result rather than crashing.
        This matches the AWS handler behaviour."""

        class BareProvider:
            pass

        action = MagicMock(provider="snowflake", action_id="a2")
        action.action_type.value = "scheduleTask"
        action.params = {}

        handler = executor._get_handler_for_provider("snowflake", BareProvider())
        result = handler._execute_single_action(action)

        assert result["status"] == "not_implemented"
        assert "scheduleTask" in result["message"]


class TestExistingProviderBranchesUnchanged:
    """Smoke tests confirming we did not regress AWS/GCP/airflow branches."""

    def test_aws_uses_generic_handler(self, executor):
        provider = MagicMock(name="aws_provider")
        handler = executor._get_handler_for_provider("aws", provider)
        assert isinstance(handler, GenericProviderActionHandler)

    def test_airflow_uses_airflow_handler(self, executor):
        provider = MagicMock(name="airflow_provider")
        handler = executor._get_handler_for_provider("airflow", provider)
        assert isinstance(handler, AirflowActionHandler)

    def test_unknown_provider_falls_back_to_generic_handler(self, executor):
        provider = MagicMock(name="some_other_provider")
        handler = executor._get_handler_for_provider("databricks", provider)
        assert isinstance(handler, GenericProviderActionHandler)
