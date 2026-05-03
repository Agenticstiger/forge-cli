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

"""Pin abstract→native op routing for AWS + GCP providers.

The 9 v0.7.1 abstract ops (``provisionDataset``, ``scheduleTask``,
``registerSchema``, ``createView``, ``grantAccess``, ``revokeAccess``,
``updatePolicy``, ``publishEvent``, ``custom``) are translated by
each provider into native ops that the provider's dispatcher must
recognise. A regression where the abstract handler emits a native
op no dispatcher knows about would silently no-op apply — these
tests prevent that by walking every abstract handler's translation
path and asserting the dispatcher accepts the resulting native op.

The tests stub out the boto3 / google-cloud actually performing the
work; only the routing is exercised. The two import sites (action
modules and provider) are both pinned.
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

# ─────────────────────── AWS ────────────────────────────────────────────


@pytest.fixture
def aws_provider():
    from fluid_build.providers.aws.provider import AwsProvider

    p = AwsProvider(account_id="123456789012", region="us-east-1")
    return p


def _make_action(action_type: str, **params) -> Dict[str, Any]:
    """Build a v0.7.1-shape action dict with the given action_type and
    params."""
    return {"action_type": action_type, "op": action_type, "params": params}


class TestAWSAbstractDispatch:
    """Each abstract op must translate to a native op the dispatcher
    routes (i.e. no ``Unknown ... operation: <op>`` raised)."""

    def test_register_schema_routes_to_glue_update_table_schema(self, aws_provider):
        """``registerSchema`` → ``glue.update_table_schema``."""
        with patch("fluid_build.providers.aws.actions.glue.update_table_schema") as mock_native:
            mock_native.return_value = {"status": "ok", "changed": False}
            action = _make_action(
                "registerSchema",
                binding={"location": {"database": "db", "table": "t"}},
                schema=[{"name": "col", "type": "string"}],
            )
            res = aws_provider._register_schema_071(action)
            assert mock_native.called
            assert res["status"] == "ok"

    def test_create_view_routes_to_athena_create_view(self, aws_provider):
        with patch("fluid_build.providers.aws.actions.athena.create_view") as mock:
            mock.return_value = {"status": "ok", "changed": False}
            action = _make_action(
                "createView",
                binding={"location": {"database": "db", "view": "v"}},
                query="SELECT 1",
            )
            res = aws_provider._create_view_071(action)
            assert mock.called

    def test_grant_access_routes_to_iam_attach_policy(self, aws_provider):
        with patch("fluid_build.providers.aws.actions.iam.attach_policy") as mock:
            mock.return_value = {"status": "ok", "changed": False}
            action = _make_action(
                "grantAccess",
                role="r",
                policy_arn="arn:aws:iam::aws:policy/foo",
            )
            res = aws_provider._grant_access_071(action)
            assert mock.called

    def test_revoke_access_routes_to_iam_detach_policy(self, aws_provider):
        """Was previously a silent no-op — ``iam.detach_policy`` did
        not exist in the dispatcher."""
        with patch("fluid_build.providers.aws.actions.iam.detach_policy") as mock:
            mock.return_value = {"status": "ok", "changed": False}
            action = _make_action(
                "revokeAccess",
                role="r",
                policy_arn="arn:aws:iam::aws:policy/foo",
            )
            res = aws_provider._revoke_access_071(action)
            assert mock.called

    def test_update_policy_routes_to_iam_put_role_policy(self, aws_provider):
        """Was previously a silent no-op — ``iam.put_role_policy`` did
        not exist in the dispatcher."""
        with patch("fluid_build.providers.aws.actions.iam.put_role_policy") as mock:
            mock.return_value = {"status": "ok", "changed": False}
            action = _make_action(
                "updatePolicy",
                role="r",
                policy_name="p",
                policy={"Version": "2012-10-17"},
            )
            res = aws_provider._update_policy_071(action)
            assert mock.called

    def test_publish_event_eventbridge_routes_to_events_put_events(self, aws_provider):
        """Was previously a silent no-op — ``events.put_events`` did
        not exist in the dispatcher."""
        with patch("fluid_build.providers.aws.actions.events.put_events") as mock:
            mock.return_value = {"status": "ok", "changed": False}
            action = _make_action(
                "publishEvent",
                source="fluid",
                detail_type="X",
                data={"k": 1},
            )
            res = aws_provider._publish_event_071(action)
            assert mock.called

    def test_publish_event_sns_transport_routes_to_sns_publish(self, aws_provider):
        with patch("fluid_build.providers.aws.actions.sns.publish_message") as mock:
            mock.return_value = {"status": "ok", "changed": False}
            action = _make_action("publishEvent", transport="sns", topic="arn:t", data={})
            res = aws_provider._publish_event_071(action)
            assert mock.called


# ─────────────────────── GCP ────────────────────────────────────────────


@pytest.fixture
def gcp_provider():
    from fluid_build.providers.gcp.provider import GcpProvider

    p = GcpProvider(project="fluid-test", region="US")
    return p


class TestGCPAbstractDispatch:
    def test_register_schema_routes_to_bq_update_table_schema(self, gcp_provider):
        with patch("fluid_build.providers.gcp.actions.bigquery.update_table_schema") as mock:
            mock.return_value = {"status": "ok", "changed": False}
            action = _make_action(
                "registerSchema",
                binding={"location": {"dataset": "d", "table": "t"}},
                schema={"properties": {"x": {"logicalType": "string"}}},
            )
            res = gcp_provider._register_schema_071(action)
            assert mock.called

    def test_create_view_routes_to_bq_ensure_view(self, gcp_provider):
        with patch("fluid_build.providers.gcp.actions.bigquery.ensure_view") as mock:
            mock.return_value = {"status": "ok", "changed": False}
            action = _make_action(
                "createView",
                binding={"location": {"dataset": "d", "view": "v"}},
                query="SELECT 1",
            )
            res = gcp_provider._create_view_071(action)
            assert mock.called

    def test_grant_access_routes_to_iam_grant_role(self, gcp_provider):
        with patch("fluid_build.providers.gcp.actions.iam.grant_role") as mock:
            mock.return_value = {"status": "ok", "changed": False}
            action = _make_action(
                "grantAccess",
                role="roles/bigquery.dataViewer",
                member="user:foo@example.com",
            )
            res = gcp_provider._grant_access_071(action)
            assert mock.called

    def test_revoke_access_routes_to_iam_revoke_role(self, gcp_provider):
        with patch("fluid_build.providers.gcp.actions.iam.revoke_role") as mock:
            mock.return_value = {"status": "ok", "changed": False}
            action = _make_action(
                "revokeAccess",
                role="roles/bigquery.dataViewer",
                member="user:foo@example.com",
            )
            res = gcp_provider._revoke_access_071(action)
            assert mock.called

    def test_update_policy_routes_to_iam_set_policy(self, gcp_provider):
        with patch("fluid_build.providers.gcp.actions.iam.set_policy") as mock:
            mock.return_value = {"status": "ok", "changed": False}
            action = _make_action(
                "updatePolicy",
                policy={"bindings": [{"role": "roles/foo", "members": ["x"]}]},
            )
            res = gcp_provider._update_policy_071(action)
            assert mock.called

    def test_publish_event_routes_to_pubsub_publish_message(self, gcp_provider):
        with patch("fluid_build.providers.gcp.actions.pubsub.publish_message") as mock:
            mock.return_value = {"status": "ok", "changed": False}
            action = _make_action(
                "publishEvent",
                topic="t",
                data={"k": 1},
            )
            res = gcp_provider._publish_event_071(action)
            assert mock.called


# ───────── Aliases & dispatcher coverage ────────────────────────────────


class TestAWSDispatcherAliases:
    """The translation layer emits two aliases the dispatcher must
    accept verbatim:

    * ``events.put_rule`` (used by ``scheduleTask``) → ``ensure_rule``.
    * ``sns.publish`` (used by ``publishEvent`` SNS transport) →
      ``publish_message``.
    """

    def test_events_put_rule_alias(self, aws_provider):
        with patch("fluid_build.providers.aws.actions.events.ensure_rule") as mock:
            mock.return_value = {"status": "ok", "changed": False}
            aws_provider._execute_events_action(
                {"op": "events.put_rule", "name": "r", "schedule": "rate(1 day)"}
            )
            assert mock.called

    def test_sns_publish_alias(self, aws_provider):
        with patch("fluid_build.providers.aws.actions.sns.publish_message") as mock:
            mock.return_value = {"status": "ok", "changed": False}
            aws_provider._execute_sns_action(
                {"op": "sns.publish", "topic": "arn:t", "message": "x"}
            )
            assert mock.called


class TestGCPDispatcherAliases:
    def test_ps_publish_alias(self, gcp_provider):
        with patch("fluid_build.providers.gcp.actions.pubsub.publish_message") as mock:
            mock.return_value = {"status": "ok", "changed": False}
            gcp_provider._execute_pubsub_action({"op": "ps.publish", "topic": "t", "data": {}})
            assert mock.called
