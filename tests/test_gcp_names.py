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

"""Tests for providers/gcp/util/names.py — GCP resource naming utilities."""

import pytest

from fluid_build.providers.gcp.util.names import (
    NamingError,
    generate_unique_name,
    normalize_bucket_name,
    normalize_composer_name,
    normalize_dataset_name,
    normalize_job_name,
    normalize_pubsub_name,
    normalize_subscription_name,
    normalize_table_name,
    normalize_topic_name,
    validate_name,
)


# ── normalize_dataset_name ──────────────────────────────────────────
class TestNormalizeDatasetName:
    def test_clean(self):
        assert normalize_dataset_name("analytics") == "analytics"

    def test_dashes_to_underscores(self):
        assert normalize_dataset_name("my-dataset") == "my_dataset"

    def test_starts_with_digit(self):
        result = normalize_dataset_name("123abc")
        assert result[0] == "_"

    def test_empty_raises(self):
        with pytest.raises(NamingError):
            normalize_dataset_name("")

    def test_long_truncated(self):
        result = normalize_dataset_name("a" * 2000)
        assert len(result) <= 1024


# ── normalize_table_name ────────────────────────────────────────────
class TestNormalizeTableName:
    def test_delegates_to_dataset(self):
        assert normalize_table_name("my-table") == normalize_dataset_name("my-table")


# ── normalize_bucket_name ───────────────────────────────────────────
class TestNormalizeBucketName:
    def test_clean(self):
        assert normalize_bucket_name("my-bucket") == "my-bucket"

    def test_uppercase_lowered(self):
        assert normalize_bucket_name("My-Bucket") == "my-bucket"

    def test_underscores_to_hyphens(self):
        assert normalize_bucket_name("my_bucket") == "my-bucket"

    def test_empty_raises(self):
        with pytest.raises(NamingError):
            normalize_bucket_name("")

    def test_too_short_padded(self):
        result = normalize_bucket_name("ab")
        assert len(result) >= 3

    def test_with_project_prefix(self):
        result = normalize_bucket_name("data", project="proj")
        assert result.startswith("proj-")

    def test_long_truncated(self):
        result = normalize_bucket_name("a" * 100)
        assert len(result) <= 63


# ── normalize_topic_name ────────────────────────────────────────────
class TestNormalizeTopicName:
    def test_clean(self):
        assert normalize_topic_name("my_topic") == "my_topic"

    def test_starts_with_digit(self):
        result = normalize_topic_name("123topic")
        assert result[0].isalpha()

    def test_empty_raises(self):
        with pytest.raises(NamingError):
            normalize_topic_name("")

    def test_short_padded(self):
        result = normalize_topic_name("ab")
        assert len(result) >= 3


# ── normalize_subscription_name / normalize_pubsub_name ─────────────
class TestSubscriptionAndPubsubName:
    def test_subscription_delegates(self):
        assert normalize_subscription_name("sub") == normalize_topic_name("sub")

    def test_pubsub_delegates(self):
        assert normalize_pubsub_name("ps") == normalize_topic_name("ps")


# ── normalize_composer_name ─────────────────────────────────────────
class TestNormalizeComposerName:
    def test_clean(self):
        assert normalize_composer_name("my-env") == "my-env"

    def test_uppercase_lowered(self):
        assert normalize_composer_name("My-Env") == "my-env"

    def test_underscores_to_hyphens(self):
        assert normalize_composer_name("my_env") == "my-env"

    def test_empty_raises(self):
        with pytest.raises(NamingError):
            normalize_composer_name("")

    def test_long_truncated(self):
        result = normalize_composer_name("a" * 100)
        assert len(result) <= 63


# ── normalize_job_name ──────────────────────────────────────────────
class TestNormalizeJobName:
    def test_clean(self):
        assert normalize_job_name("myjob") == "myjob"

    def test_starts_with_digit(self):
        result = normalize_job_name("123job")
        assert result[0].isalpha()

    def test_empty_raises(self):
        with pytest.raises(NamingError):
            normalize_job_name("")

    def test_short_padded(self):
        result = normalize_job_name("ab")
        assert len(result) >= 4

    def test_long_truncated(self):
        result = normalize_job_name("a" * 100)
        assert len(result) <= 63


# ── validate_name ───────────────────────────────────────────────────
class TestValidateName:
    def test_valid_dataset(self):
        assert validate_name("my_dataset", "dataset") is True

    def test_invalid_dataset_dash(self):
        assert validate_name("my-dataset", "dataset") is False

    def test_valid_bucket(self):
        assert validate_name("my-bucket", "bucket") is True

    def test_bucket_too_short(self):
        assert validate_name("ab", "bucket") is False

    def test_valid_topic(self):
        assert validate_name("my_topic", "topic") is True

    def test_topic_starts_digit(self):
        assert validate_name("1topic", "topic") is False

    def test_valid_job(self):
        assert validate_name("myjob", "job") is True

    def test_job_starts_uppercase(self):
        assert validate_name("Myjob", "job") is False

    def test_unknown_resource_type(self):
        assert validate_name("name", "unknown") is False


# ── generate_unique_name ────────────────────────────────────────────
class TestGenerateUniqueName:
    def test_no_conflict(self):
        result = generate_unique_name("analytics", "dataset")
        assert result == "analytics"

    def test_conflict_adds_suffix(self):
        result = generate_unique_name("analytics", "dataset", existing_names=["analytics"])
        assert result.startswith("analytics")
        assert result != "analytics"

    def test_unknown_type_raises(self):
        with pytest.raises(NamingError, match="Unknown resource type"):
            generate_unique_name("x", "unknown_type")

    def test_bucket_with_project(self):
        result = generate_unique_name("data", "bucket", project="proj")
        assert "proj" in result
