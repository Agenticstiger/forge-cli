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

# fluid_build/providers/gcp/actions/pubsub.py
"""
Google Cloud Pub/Sub actions for GCP provider.

Implements idempotent Pub/Sub operations including:
- Topic creation and management
- Subscription creation and configuration
- Dead letter queue setup
- IAM policy bindings
- Message publishing and acknowledgment settings
"""

import time
from typing import Any, Dict

from fluid_build.providers.base import ProviderError

from ..util.logging import duration_ms
from ..util.names import normalize_pubsub_name


def ensure_topic(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure Pub/Sub topic exists with specified configuration.

    Creates topic if it doesn't exist, updates configuration if changed.
    Idempotent operation - safe to run multiple times.

    Args:
        action: Topic action configuration

    Returns:
        Action result with status and details
    """
    start_time = time.time()

    try:
        from google.cloud import pubsub_v1
        from google.cloud.exceptions import AlreadyExists, NotFound
    except ImportError:
        return {
            "status": "error",
            "error": "google-cloud-pubsub library not available. Install with: pip install google-cloud-pubsub",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    project = action.get("project")
    topic_name = action.get("topic")
    labels = action.get("labels", {})
    message_retention_duration = action.get("message_retention_duration")
    schema_settings = action.get("schema_settings")

    if not project or not topic_name:
        return {
            "status": "error",
            "error": "Both 'project' and 'topic' are required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        # Initialize Pub/Sub client
        publisher = pubsub_v1.PublisherClient()

        # Normalize topic name
        normalized_topic = normalize_pubsub_name(topic_name)
        topic_path = publisher.topic_path(project, normalized_topic)

        changed = False

        try:
            # Check if topic exists
            existing_topic = publisher.get_topic(request={"topic": topic_path})

            # Compare and update if necessary
            update_mask = []

            # Check labels
            existing_labels = dict(existing_topic.labels) if existing_topic.labels else {}
            if existing_labels != labels:
                existing_topic.labels.clear()
                existing_topic.labels.update(labels)
                update_mask.append("labels")
                changed = True

            # Check message retention duration
            if message_retention_duration:
                from google.protobuf.duration_pb2 import Duration

                duration = Duration()
                duration.FromSeconds(message_retention_duration)

                if existing_topic.message_retention_duration != duration:
                    existing_topic.message_retention_duration.CopyFrom(duration)
                    update_mask.append("message_retention_duration")
                    changed = True

            # Check schema settings
            if schema_settings:
                schema_config = _convert_schema_settings(schema_settings, project)
                if existing_topic.schema_settings != schema_config:
                    existing_topic.schema_settings.CopyFrom(schema_config)
                    update_mask.append("schema_settings")
                    changed = True

            if update_mask:
                from google.protobuf.field_mask_pb2 import FieldMask

                update_request = {
                    "topic": existing_topic,
                    "update_mask": FieldMask(paths=update_mask),
                }
                publisher.update_topic(request=update_request)

            return {
                "status": "changed" if changed else "ok",
                "topic_name": existing_topic.name,
                "labels": dict(existing_topic.labels),
                "message_retention_duration": (
                    existing_topic.message_retention_duration.seconds
                    if existing_topic.message_retention_duration
                    else None
                ),
                "duration_ms": duration_ms(start_time),
                "changed": changed,
            }

        except NotFound:
            # Topic doesn't exist, create it
            from google.cloud.pubsub_v1.types import Topic

            topic = Topic()
            topic.name = topic_path
            topic.labels.update(labels)

            # Set message retention duration if specified
            if message_retention_duration:
                from google.protobuf.duration_pb2 import Duration

                duration = Duration()
                duration.FromSeconds(message_retention_duration)
                topic.message_retention_duration.CopyFrom(duration)

            # Set schema settings if specified
            if schema_settings:
                schema_config = _convert_schema_settings(schema_settings, project)
                topic.schema_settings.CopyFrom(schema_config)

            created_topic = publisher.create_topic(request={"name": topic_path, "topic": topic})

            return {
                "status": "changed",
                "topic_name": created_topic.name,
                "labels": dict(created_topic.labels),
                "message_retention_duration": (
                    created_topic.message_retention_duration.seconds
                    if created_topic.message_retention_duration
                    else None
                ),
                "duration_ms": duration_ms(start_time),
                "changed": True,
            }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "topic_name": topic_path if "topic_path" in locals() else None,
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def ensure_subscription(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure Pub/Sub subscription exists with specified configuration.

    Creates subscription if it doesn't exist, updates configuration if changed.
    Supports dead letter queues, retry policies, and filtering.

    Args:
        action: Subscription action configuration

    Returns:
        Action result with status and details
    """
    start_time = time.time()

    try:
        from google.cloud import pubsub_v1
        from google.cloud.exceptions import NotFound
    except ImportError:
        return {
            "status": "error",
            "error": "google-cloud-pubsub library not available. Install with: pip install google-cloud-pubsub",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    project = action.get("project")
    subscription_name = action.get("subscription")
    topic_name = action.get("topic")
    labels = action.get("labels", {})
    ack_deadline_seconds = action.get("ack_deadline_seconds", 10)
    message_retention_duration = action.get("message_retention_duration")
    retain_acked_messages = action.get("retain_acked_messages", False)
    filter_expression = action.get("filter")
    dead_letter_policy = action.get("dead_letter_policy")
    retry_policy = action.get("retry_policy")
    push_config = action.get("push_config")

    if not all([project, subscription_name, topic_name]):
        return {
            "status": "error",
            "error": "Project, subscription, and topic are all required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        # Initialize Pub/Sub client
        subscriber = pubsub_v1.SubscriberClient()
        publisher = pubsub_v1.PublisherClient()

        # Normalize names
        normalized_subscription = normalize_pubsub_name(subscription_name)
        normalized_topic = normalize_pubsub_name(topic_name)

        subscription_path = subscriber.subscription_path(project, normalized_subscription)
        topic_path = publisher.topic_path(project, normalized_topic)

        changed = False

        try:
            # Check if subscription exists
            existing_subscription = subscriber.get_subscription(
                request={"subscription": subscription_path}
            )

            # Compare and update if necessary
            update_mask = []

            # Check labels
            existing_labels = (
                dict(existing_subscription.labels) if existing_subscription.labels else {}
            )
            if existing_labels != labels:
                existing_subscription.labels.clear()
                existing_subscription.labels.update(labels)
                update_mask.append("labels")
                changed = True

            # Check ack deadline
            if existing_subscription.ack_deadline_seconds != ack_deadline_seconds:
                existing_subscription.ack_deadline_seconds = ack_deadline_seconds
                update_mask.append("ack_deadline_seconds")
                changed = True

            # Check message retention duration
            if message_retention_duration:
                from google.protobuf.duration_pb2 import Duration

                duration = Duration()
                duration.FromSeconds(message_retention_duration)

                if existing_subscription.message_retention_duration != duration:
                    existing_subscription.message_retention_duration.CopyFrom(duration)
                    update_mask.append("message_retention_duration")
                    changed = True

            # Check retain acked messages
            if existing_subscription.retain_acked_messages != retain_acked_messages:
                existing_subscription.retain_acked_messages = retain_acked_messages
                update_mask.append("retain_acked_messages")
                changed = True

            # Check filter expression
            current_filter = existing_subscription.filter if existing_subscription.filter else ""
            new_filter = filter_expression if filter_expression else ""

            if current_filter != new_filter:
                existing_subscription.filter = new_filter
                update_mask.append("filter")
                changed = True

            # Check dead letter policy
            if dead_letter_policy:
                dlq_config = _convert_dead_letter_policy(dead_letter_policy, project)
                if existing_subscription.dead_letter_policy != dlq_config:
                    existing_subscription.dead_letter_policy.CopyFrom(dlq_config)
                    update_mask.append("dead_letter_policy")
                    changed = True

            # Check retry policy
            if retry_policy:
                retry_config = _convert_retry_policy(retry_policy)
                if existing_subscription.retry_policy != retry_config:
                    existing_subscription.retry_policy.CopyFrom(retry_config)
                    update_mask.append("retry_policy")
                    changed = True

            # Check push config
            if push_config:
                push_configuration = _convert_push_config(push_config)
                if existing_subscription.push_config != push_configuration:
                    existing_subscription.push_config.CopyFrom(push_configuration)
                    update_mask.append("push_config")
                    changed = True

            if update_mask:
                from google.protobuf.field_mask_pb2 import FieldMask

                update_request = {
                    "subscription": existing_subscription,
                    "update_mask": FieldMask(paths=update_mask),
                }
                subscriber.update_subscription(request=update_request)

            return {
                "status": "changed" if changed else "ok",
                "subscription_name": existing_subscription.name,
                "topic_name": existing_subscription.topic,
                "labels": dict(existing_subscription.labels),
                "ack_deadline_seconds": existing_subscription.ack_deadline_seconds,
                "filter": existing_subscription.filter if existing_subscription.filter else None,
                "duration_ms": duration_ms(start_time),
                "changed": changed,
            }

        except NotFound:
            # Subscription doesn't exist, create it
            from google.cloud.pubsub_v1.types import Subscription

            subscription = Subscription()
            subscription.name = subscription_path
            subscription.topic = topic_path
            subscription.labels.update(labels)
            subscription.ack_deadline_seconds = ack_deadline_seconds
            subscription.retain_acked_messages = retain_acked_messages

            if filter_expression:
                subscription.filter = filter_expression

            # Set message retention duration if specified
            if message_retention_duration:
                from google.protobuf.duration_pb2 import Duration

                duration = Duration()
                duration.FromSeconds(message_retention_duration)
                subscription.message_retention_duration.CopyFrom(duration)

            # Set dead letter policy if specified
            if dead_letter_policy:
                dlq_config = _convert_dead_letter_policy(dead_letter_policy, project)
                subscription.dead_letter_policy.CopyFrom(dlq_config)

            # Set retry policy if specified
            if retry_policy:
                retry_config = _convert_retry_policy(retry_policy)
                subscription.retry_policy.CopyFrom(retry_config)

            # Set push config if specified
            if push_config:
                push_configuration = _convert_push_config(push_config)
                subscription.push_config.CopyFrom(push_configuration)

            created_subscription = subscriber.create_subscription(
                request={"name": subscription_path, "subscription": subscription}
            )

            return {
                "status": "changed",
                "subscription_name": created_subscription.name,
                "topic_name": created_subscription.topic,
                "labels": dict(created_subscription.labels),
                "ack_deadline_seconds": created_subscription.ack_deadline_seconds,
                "filter": created_subscription.filter if created_subscription.filter else None,
                "duration_ms": duration_ms(start_time),
                "changed": True,
            }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "subscription_name": subscription_path if "subscription_path" in locals() else None,
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def ensure_topic_iam_policy(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure IAM policy bindings are set on topic.

    Adds or removes IAM policy bindings for topic access control.

    Args:
        action: IAM policy action configuration

    Returns:
        Action result with status and details
    """
    start_time = time.time()

    try:
        from google.cloud import pubsub_v1
        from google.cloud.exceptions import NotFound
    except ImportError:
        return {
            "status": "error",
            "error": "google-cloud-pubsub library not available. Install with: pip install google-cloud-pubsub",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    project = action.get("project")
    topic_name = action.get("topic")
    bindings = action.get("bindings", [])

    if not all([project, topic_name]):
        return {
            "status": "error",
            "error": "Both 'project' and 'topic' are required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        # Initialize Pub/Sub client
        publisher = pubsub_v1.PublisherClient()

        # Normalize topic name
        normalized_topic = normalize_pubsub_name(topic_name)
        topic_path = publisher.topic_path(project, normalized_topic)

        try:
            # Get current IAM policy
            policy = publisher.get_iam_policy(request={"resource": topic_path})

            changed = False

            # Apply bindings
            for binding in bindings:
                role = binding.get("role")
                members = binding.get("members", [])
                condition = binding.get("condition")

                if not role or not members:
                    continue

                # Check if binding already exists
                existing_binding = None
                for existing in policy.bindings:
                    if existing.role == role:
                        # Check if condition matches (if specified)
                        if condition:
                            if hasattr(existing, "condition") and existing.condition == condition:
                                existing_binding = existing
                                break
                        else:
                            if not hasattr(existing, "condition") or not existing.condition:
                                existing_binding = existing
                                break

                if existing_binding:
                    # Update existing binding
                    current_members = set(existing_binding.members)
                    new_members = set(members)

                    if current_members != new_members:
                        existing_binding.members[:] = list(new_members)
                        changed = True
                else:
                    # Add new binding
                    from google.cloud.pubsub_v1.types import Binding

                    new_binding = Binding()
                    new_binding.role = role
                    new_binding.members[:] = members

                    if condition:
                        from google.type.expr_pb2 import Expr

                        condition_expr = Expr()
                        condition_expr.expression = condition.get("expression", "")
                        condition_expr.title = condition.get("title", "")
                        condition_expr.description = condition.get("description", "")
                        new_binding.condition.CopyFrom(condition_expr)

                    policy.bindings.append(new_binding)
                    changed = True

            if changed:
                publisher.set_iam_policy(request={"resource": topic_path, "policy": policy})

            return {
                "status": "changed" if changed else "ok",
                "topic_name": topic_path,
                "bindings_count": len(policy.bindings),
                "duration_ms": duration_ms(start_time),
                "changed": changed,
            }

        except NotFound:
            return {
                "status": "error",
                "error": f"Topic {topic_path} does not exist",
                "duration_ms": duration_ms(start_time),
                "changed": False,
            }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "topic_name": topic_path if "topic_path" in locals() else None,
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def publish_message(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Publish message to Pub/Sub topic.

    Publishes single message with optional attributes and ordering key.

    Args:
        action: Message publishing action configuration

    Returns:
        Action result with message ID and publish details
    """
    start_time = time.time()

    try:
        from google.cloud import pubsub_v1
        from google.cloud.exceptions import NotFound
    except ImportError:
        return {
            "status": "error",
            "error": "google-cloud-pubsub library not available. Install with: pip install google-cloud-pubsub",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    project = action.get("project")
    topic_name = action.get("topic")
    data = action.get("data")
    attributes = action.get("attributes", {})
    ordering_key = action.get("ordering_key")

    if not all([project, topic_name, data]):
        return {
            "status": "error",
            "error": "Project, topic, and data are all required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        # Initialize Pub/Sub client
        publisher = pubsub_v1.PublisherClient()

        # Normalize topic name
        normalized_topic = normalize_pubsub_name(topic_name)
        topic_path = publisher.topic_path(project, normalized_topic)

        # Prepare message data
        if isinstance(data, str):
            message_data = data.encode("utf-8")
        else:
            message_data = data

        # Publish message
        publish_kwargs = {
            "data": message_data,
        }

        if attributes:
            publish_kwargs["attributes"] = attributes

        if ordering_key:
            publish_kwargs["ordering_key"] = ordering_key

        future = publisher.publish(topic_path, **publish_kwargs)
        message_id = future.result()  # Block until message is published

        return {
            "status": "ok",
            "topic_name": topic_path,
            "message_id": message_id,
            "data_size": len(message_data),
            "attributes_count": len(attributes),
            "ordering_key": ordering_key,
            "duration_ms": duration_ms(start_time),
            "changed": True,  # Publishing always makes a change
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "topic_name": topic_path if "topic_path" in locals() else None,
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def create_schema(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Create Pub/Sub schema for message validation.

    Creates schema if it doesn't exist, updates if definition changed.

    Args:
        action: Schema creation action configuration

    Returns:
        Action result with schema details
    """
    start_time = time.time()

    try:
        from google.cloud import pubsub_v1
        from google.cloud.exceptions import AlreadyExists, NotFound
    except ImportError:
        return {
            "status": "error",
            "error": "google-cloud-pubsub library not available. Install with: pip install google-cloud-pubsub",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    project = action.get("project")
    schema_name = action.get("schema")
    schema_type = action.get("type", "AVRO")  # AVRO or PROTOCOL_BUFFER
    definition = action.get("definition")

    if not all([project, schema_name, definition]):
        return {
            "status": "error",
            "error": "Project, schema, and definition are all required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        # Initialize Schema client
        schema_client = pubsub_v1.SchemaServiceClient()

        # Normalize schema name
        normalized_schema = normalize_pubsub_name(schema_name)
        schema_path = schema_client.schema_path(project, normalized_schema)
        parent = schema_client.common_project_path(project)

        changed = False

        try:
            # Check if schema exists
            existing_schema = schema_client.get_schema(request={"name": schema_path})

            # Compare definition
            if existing_schema.definition != definition:
                # Update schema (create new revision)
                from google.cloud.pubsub_v1.types import Schema

                updated_schema = Schema()
                updated_schema.name = schema_path
                updated_schema.type_ = getattr(Schema.Type, schema_type)
                updated_schema.definition = definition

                # Note: Schemas are immutable, so we need to delete and recreate
                # or create a new revision (if supported)
                schema_client.delete_schema(request={"name": schema_path})
                created_schema = schema_client.create_schema(
                    request={
                        "parent": parent,
                        "schema": updated_schema,
                        "schema_id": normalized_schema,
                    }
                )
                changed = True
            else:
                created_schema = existing_schema

            return {
                "status": "changed" if changed else "ok",
                "schema_name": created_schema.name,
                "schema_type": created_schema.type_.name,
                "definition_size": len(created_schema.definition),
                "duration_ms": duration_ms(start_time),
                "changed": changed,
            }

        except NotFound:
            # Schema doesn't exist, create it
            from google.cloud.pubsub_v1.types import Schema

            schema = Schema()
            schema.name = schema_path
            schema.type_ = getattr(Schema.Type, schema_type)
            schema.definition = definition

            created_schema = schema_client.create_schema(
                request={"parent": parent, "schema": schema, "schema_id": normalized_schema}
            )

            return {
                "status": "changed",
                "schema_name": created_schema.name,
                "schema_type": created_schema.type_.name,
                "definition_size": len(created_schema.definition),
                "duration_ms": duration_ms(start_time),
                "changed": True,
            }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "schema_name": schema_path if "schema_path" in locals() else None,
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


# Helper functions


def _convert_schema_settings(schema_settings: Dict[str, Any], project: str):
    """Convert schema settings to Pub/Sub format."""
    try:
        from google.cloud import pubsub_v1
        from google.cloud.pubsub_v1.types import SchemaSettings
    except ImportError:
        raise ProviderError("google-cloud-pubsub library not available")

    schema_client = pubsub_v1.SchemaServiceClient()
    schema_name = schema_settings.get("schema")
    encoding = schema_settings.get("encoding", "ENCODING_UNSPECIFIED")

    settings = SchemaSettings()
    if schema_name:
        settings.schema = schema_client.schema_path(project, schema_name)

    if encoding:
        settings.encoding = getattr(SchemaSettings.Encoding, encoding)

    return settings


def _convert_dead_letter_policy(dead_letter_policy: Dict[str, Any], project: str):
    """Convert dead letter policy to Pub/Sub format."""
    try:
        from google.cloud import pubsub_v1
        from google.cloud.pubsub_v1.types import DeadLetterPolicy
    except ImportError:
        raise ProviderError("google-cloud-pubsub library not available")

    publisher = pubsub_v1.PublisherClient()

    policy = DeadLetterPolicy()

    dead_letter_topic = dead_letter_policy.get("dead_letter_topic")
    if dead_letter_topic:
        normalized_topic = normalize_pubsub_name(dead_letter_topic)
        policy.dead_letter_topic = publisher.topic_path(project, normalized_topic)

    max_delivery_attempts = dead_letter_policy.get("max_delivery_attempts", 5)
    policy.max_delivery_attempts = max_delivery_attempts

    return policy


def _convert_retry_policy(retry_policy: Dict[str, Any]):
    """Convert retry policy to Pub/Sub format."""
    try:
        from google.cloud.pubsub_v1.types import RetryPolicy
        from google.protobuf.duration_pb2 import Duration
    except ImportError:
        raise ProviderError("google-cloud-pubsub library not available")

    policy = RetryPolicy()

    min_backoff = retry_policy.get("minimum_backoff")
    if min_backoff:
        min_duration = Duration()
        min_duration.FromSeconds(min_backoff)
        policy.minimum_backoff.CopyFrom(min_duration)

    max_backoff = retry_policy.get("maximum_backoff")
    if max_backoff:
        max_duration = Duration()
        max_duration.FromSeconds(max_backoff)
        policy.maximum_backoff.CopyFrom(max_duration)

    return policy


def _convert_push_config(push_config: Dict[str, Any]):
    """Convert push config to Pub/Sub format."""
    try:
        from google.cloud.pubsub_v1.types import PushConfig
    except ImportError:
        raise ProviderError("google-cloud-pubsub library not available")

    config = PushConfig()

    push_endpoint = push_config.get("push_endpoint")
    if push_endpoint:
        config.push_endpoint = push_endpoint

    attributes = push_config.get("attributes", {})
    if attributes:
        config.attributes.update(attributes)

    oidc_token = push_config.get("oidc_token")
    if oidc_token:
        config.oidc_token.service_account_email = oidc_token.get("service_account_email", "")
        config.oidc_token.audience = oidc_token.get("audience", "")

    return config
