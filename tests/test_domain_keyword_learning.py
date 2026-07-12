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

"""Domain-keyword learning: frecency-ranked usage history in ``ai_config.json``.

Pins the ``_ai_setup_storage`` helpers added for the domain-keyword-learning
card:

* :func:`record_domain_detection` — accumulate ``count`` + ``last_seen`` per
  domain SLUG, **without clobbering** provider entries / the active marker /
  sibling domains (read-modify-write).
* :func:`suggest_domain_template` — frecency (frequency + recency) nudge once a
  domain crosses the repeat threshold; ``None`` before.
* :func:`get_domain_history` — read-only snapshot.

Privacy invariant: only the domain slug + count + timestamp are ever written —
never prompt text, contract content, paths, or PII.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def iso_config(tmp_path, monkeypatch):
    """Point the AI-config path at a throwaway file (no real ``~/.fluid``)."""
    config_file = tmp_path / "ai_config.json"
    monkeypatch.setattr("fluid_build.cli.ai_setup._CONFIG_FILE", config_file)
    monkeypatch.setattr("fluid_build.cli.ai_setup._CONFIG_DIR", tmp_path)
    monkeypatch.delenv("FLUID_ALLOW_PLAINTEXT_AI_SECRETS", raising=False)
    return config_file


@pytest.fixture
def keyring_mem(monkeypatch):
    """In-memory keyring so ``_save_ai_config`` never touches the OS keychain."""
    store: dict = {}
    from fluid_build.credentials.keyring_store import KeyringCredentialStore

    monkeypatch.setattr(
        KeyringCredentialStore, "set_credential", lambda key, val: store.__setitem__(key, val)
    )
    monkeypatch.setattr(KeyringCredentialStore, "get_credential", lambda key: store.get(key))
    monkeypatch.setattr(
        KeyringCredentialStore, "delete_credential", lambda key: store.pop(key, None)
    )
    return store


# A context that trips ``detect_domain`` → "finance" (>= 2 keyword hits).
FINANCE_CTX = {
    "project_goal": "banking fraud detection",
    "description": "payment risk and regulatory compliance for a fintech",
}
HEALTHCARE_CTX = {
    "project_goal": "clinical patient records",
    "description": "hospital EHR with HIPAA compliance for medical claims",
}


class TestRecordDomainDetection:
    def test_context_accumulates_counts_and_timestamps(self, iso_config):
        from fluid_build.cli._ai_setup_storage import record_domain_detection

        assert record_domain_detection(FINANCE_CTX) == "finance"
        assert record_domain_detection(FINANCE_CTX) == "finance"

        data = json.loads(iso_config.read_text())
        hist = data["domain_history"]
        assert hist["finance"]["count"] == 2
        # last_seen is an ISO-8601 UTC timestamp we can round-trip.
        datetime.fromisoformat(hist["finance"]["last_seen"])

    def test_accepts_resolved_slug_directly(self, iso_config):
        from fluid_build.cli._ai_setup_storage import get_domain_history, record_domain_detection

        assert record_domain_detection("finance") == "finance"
        assert get_domain_history()["finance"]["count"] == 1

    def test_unresolved_or_invalid_domain_is_noop(self, iso_config):
        from fluid_build.cli._ai_setup_storage import record_domain_detection

        # A context with no domain keywords resolves to nothing.
        assert record_domain_detection({"project_goal": "hello world widgets"}) is None
        # Non-slug junk (defence-in-depth privacy guard) is rejected.
        assert record_domain_detection("../etc/passwd") is None
        assert record_domain_detection("a domain with spaces") is None
        assert record_domain_detection(12345) is None
        assert not iso_config.exists()  # nothing was written

    def test_only_slug_count_timestamp_persisted_no_pii(self, iso_config):
        """Privacy: the raw prompt text must never reach the config file."""
        from fluid_build.cli._ai_setup_storage import record_domain_detection

        secret_ctx = {
            "project_goal": "banking fraud detection",
            "description": "payment risk compliance",
            "data_sources": "s3://acme-secret-bucket/pii/customers.csv",
            "owner_email": "ceo@acme.example",
        }
        record_domain_detection(secret_ctx)

        raw = iso_config.read_text()
        assert "finance" in raw
        assert "acme-secret-bucket" not in raw
        assert "ceo@acme.example" not in raw
        assert "banking fraud detection" not in raw
        entry = json.loads(raw)["domain_history"]["finance"]
        assert set(entry.keys()) == {"count", "last_seen"}


class TestMergeNotClobber:
    def test_record_preserves_prior_save_ai_config(self, iso_config, keyring_mem):
        """KEY REGRESSION: recording domains must not erase provider entries."""
        from fluid_build.cli._ai_setup_storage import (
            _load_ai_config_map,
            _save_ai_config,
            record_domain_detection,
        )

        # A prior, unrelated multi-provider save.
        assert _save_ai_config("openai", "gpt-4o", api_key="sk-openai")
        assert _save_ai_config("gemini", "gemini-2.5-pro", api_key="AIza-gemini")

        # Now accumulate a domain twice.
        assert record_domain_detection(FINANCE_CTX) == "finance"
        assert record_domain_detection(FINANCE_CTX) == "finance"

        data = json.loads(iso_config.read_text())
        # domain_history accumulated …
        assert data["domain_history"]["finance"]["count"] == 2
        # … AND every provider key + the active marker survived (merge, not clobber).
        assert set(data["providers"].keys()) == {"openai", "gemini"}
        assert data["active"] == "gemini"
        assert data["providers"]["openai"]["model"] == "gpt-4o"
        assert data["version"] == 2

        # The provider-facing readers still work unchanged.
        cfg_map = _load_ai_config_map()
        assert set(cfg_map["providers"].keys()) == {"openai", "gemini"}

    def test_save_ai_config_after_history_preserves_history(self, iso_config, keyring_mem):
        """Reverse direction: a provider save must not erase domain history."""
        from fluid_build.cli._ai_setup_storage import _save_ai_config, record_domain_detection

        record_domain_detection(FINANCE_CTX)
        assert _save_ai_config("openai", "gpt-4o", api_key="sk-openai")

        data = json.loads(iso_config.read_text())
        assert data["domain_history"]["finance"]["count"] == 1
        assert data["providers"]["openai"]["model"] == "gpt-4o"


class TestSuggestDomainTemplate:
    def test_none_before_threshold(self, iso_config):
        from fluid_build.cli._ai_setup_storage import (
            record_domain_detection,
            suggest_domain_template,
        )

        record_domain_detection("finance")
        record_domain_detection("finance")
        assert suggest_domain_template() is None  # count 2 < threshold 3

    def test_suggests_after_threshold(self, iso_config):
        from fluid_build.cli._ai_setup_storage import (
            record_domain_detection,
            suggest_domain_template,
        )

        for _ in range(3):
            record_domain_detection("finance")
        assert suggest_domain_template() == "finance"

    def test_excludes_active_domain(self, iso_config):
        from fluid_build.cli._ai_setup_storage import (
            record_domain_detection,
            suggest_domain_template,
        )

        for _ in range(3):
            record_domain_detection("finance")
        # If finance is already the run's active domain, don't nag about it.
        assert suggest_domain_template(exclude="finance") is None

    def test_frecency_recent_beats_stale(self, iso_config, monkeypatch):
        """A recently-built domain out-ranks a heavier but stale one."""
        from fluid_build.cli import _ai_setup_storage as store

        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # "finance": built 5x, but ~6 months ago (heavily decayed).
        for _ in range(5):
            store.record_domain_detection("finance", now=base)
        # "healthcare": built 3x, just now.
        recent = base + timedelta(days=180)
        for _ in range(3):
            store.record_domain_detection("healthcare", now=recent)

        # Both are over threshold; recency tips it to healthcare.
        assert store.suggest_domain_template(now=recent) == "healthcare"
        # Confirm the raw counts really are finance-heavy (so it's frecency, not count).
        hist = store.get_domain_history()
        assert hist["finance"]["count"] == 5
        assert hist["healthcare"]["count"] == 3


class TestGetDomainHistory:
    def test_empty_when_unconfigured(self, iso_config):
        from fluid_build.cli._ai_setup_storage import get_domain_history

        assert get_domain_history() == {}

    def test_returns_copy_not_live_ref(self, iso_config):
        from fluid_build.cli._ai_setup_storage import get_domain_history, record_domain_detection

        record_domain_detection("finance")
        snap = get_domain_history()
        snap["finance"]["count"] = 999
        # Mutating the snapshot must not have touched the file.
        assert get_domain_history()["finance"]["count"] == 1
