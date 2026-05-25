# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Shadow-compare — parity between the native and OpenTofu apply engines.

Phase 3 of the OpenTofu migration runs both engines side by side before
flipping any provider's default (the strangler-fig pattern): the native
planner and the OpenTofu emitter consume the *same* contract, and their
intent is diffed. A provider is safe to cut over only once OpenTofu
plans every logical resource the native engine does — i.e. there are no
``native_only`` gaps.

The diff is at the granularity of a :class:`LogicalResource` — a
``(kind, identity)`` pair such as ``("table", "orders")`` — so the two
engines' very different vocabularies (native action ``op`` strings vs
OpenTofu resource-type names) line up apples-to-apples. Kind detection
is best-effort; the authoritative gate remains a real ``tofu plan`` diff
on a real cloud (see ``AUTOGEN_SPIKE.md``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Set, Tuple

from .base import IacProviderPlugin


@dataclass(frozen=True, order=True)
class LogicalResource:
    """A provisioned thing, normalised across both engines' vocabularies."""

    kind: str
    identity: str


# OpenTofu resource type → logical kind.
_TF_TYPE_TO_KIND = {
    "aws_glue_catalog_database": "database",
    "aws_glue_catalog_table": "table",
    "aws_s3_bucket": "bucket",
    "aws_kinesis_stream": "stream",
    "google_bigquery_dataset": "dataset",
    "google_bigquery_table": "table",
    "google_storage_bucket": "bucket",
    "google_pubsub_topic": "topic",
    "snowflake_database": "database",
    "snowflake_schema": "schema",
    "snowflake_table": "table",
    "snowflake_view": "view",
}

# Native action `op` → logical kind, matched by keyword so namespaced ops
# (`glue.create_table`, `bq.ensure_dataset`) all resolve. More-specific
# words come first so `update_table_schema` resolves to `table`.
_OP_KEYWORDS: Tuple[Tuple[str, str], ...] = (
    ("dataset", "dataset"),
    ("database", "database"),
    ("table", "table"),
    ("view", "view"),
    ("schema", "schema"),
    ("bucket", "bucket"),
    ("topic", "topic"),
    ("stream", "stream"),
)

_TF_NAME_FIELDS = ("name", "bucket", "table_id", "dataset_id", "topic", "stream")
_NATIVE_NAME_FIELDS = (
    "table",
    "view",
    "bucket",
    "schema",
    "dataset",
    "database",
    "topic",
    "stream",
    "name",
)


def _tf_kind(tf_type: str, body: Any) -> str:
    """Logical kind for an OpenTofu resource (the body distinguishes BQ views)."""
    if tf_type == "google_bigquery_table" and isinstance(body, Mapping) and "view" in body:
        return "view"
    return _TF_TYPE_TO_KIND.get(tf_type, tf_type)


def _tf_identity(body: Any) -> str:
    """A stable leaf identity from a resource body — never an interpolation."""
    if isinstance(body, Mapping):
        for field in _TF_NAME_FIELDS:
            value = body.get(field)
            if isinstance(value, str) and value and not value.startswith("${"):
                return value
    return ""


def _native_kind(op: Any) -> Optional[str]:
    """Logical kind for a native action ``op``.

    Returns ``None`` when the op does not provision a declarative
    resource (imperative ops like ``publishEvent``/``grant``) — those are
    out of scope for the parity diff (``AUTOGEN_SPIKE.md`` risk R8).
    """
    text = str(op or "").lower()
    for keyword, kind in _OP_KEYWORDS:
        if keyword in text:
            return kind
    return None


def _native_identity(action: Mapping[str, Any]) -> str:
    """A leaf identity for a native action."""
    for field in _NATIVE_NAME_FIELDS:
        value = action.get(field)
        if isinstance(value, str) and value:
            return value
    return ""


def opentofu_logical_resources(
    contract: Mapping[str, Any],
    plugin: IacProviderPlugin,
    native_actions: Iterable[Mapping[str, Any]] = (),
) -> Set[LogicalResource]:
    """The logical resources the OpenTofu emitter would provision.

    ``native_actions`` is threaded into ``emit`` so the emitter's
    action-driven (schedule / orchestration) resources are compared too.
    """
    out: Set[LogicalResource] = set()
    for tf_type, named in plugin.emit(contract, native_actions).items():
        for body in (named or {}).values():
            out.add(LogicalResource(_tf_kind(tf_type, body), _tf_identity(body)))
    return out


def native_logical_resources(actions: Iterable[Mapping[str, Any]]) -> Set[LogicalResource]:
    """The logical resources a native ``provider.plan()`` would provision.

    Non-provisioning actions (imperative ops with no declarative form)
    are skipped — see :func:`_native_kind`.
    """
    out: Set[LogicalResource] = set()
    for action in actions or []:
        if not isinstance(action, Mapping):
            continue
        kind = _native_kind(action.get("op") or action.get("action_type"))
        if kind is not None:
            out.add(LogicalResource(kind, _native_identity(action)))
    return out


@dataclass(frozen=True)
class ShadowReport:
    """The parity diff between the two engines for one contract."""

    provider: str
    matched: Tuple[LogicalResource, ...]
    opentofu_only: Tuple[LogicalResource, ...]
    native_only: Tuple[LogicalResource, ...]

    @property
    def parity_pct(self) -> float:
        """Share (0–100) of logical resources both engines agree on."""
        total = len(self.matched) + len(self.opentofu_only) + len(self.native_only)
        return 100.0 if total == 0 else round(100.0 * len(self.matched) / total, 1)

    @property
    def ok(self) -> bool:
        """True when OpenTofu covers every resource the native engine plans.

        ``opentofu_only`` extras are fine — the emitter is simply ahead.
        A ``native_only`` gap means OpenTofu would miss something, so the
        provider is **not** yet safe to cut over.
        """
        return not self.native_only

    def summary(self) -> str:
        """One-line human summary."""
        verdict = "cutover-safe" if self.ok else "GAPS — not cutover-safe"
        return (
            f"{self.provider}: parity {self.parity_pct}% — "
            f"{len(self.matched)} matched, {len(self.opentofu_only)} opentofu-only, "
            f"{len(self.native_only)} native-only — {verdict}"
        )


def shadow_compare(
    contract: Mapping[str, Any],
    *,
    plugin: IacProviderPlugin,
    native_actions: Iterable[Mapping[str, Any]],
) -> ShadowReport:
    """Diff the OpenTofu emitter against the native planner for one contract.

    ``native_actions`` is the output of the native ``provider.plan()`` —
    passed in so this function stays pure (no provider construction, no
    credentials, no network).
    """
    tf = opentofu_logical_resources(contract, plugin, native_actions)
    native = native_logical_resources(native_actions)
    return ShadowReport(
        provider=getattr(plugin, "name", "?"),
        matched=tuple(sorted(tf & native)),
        opentofu_only=tuple(sorted(tf - native)),
        native_only=tuple(sorted(native - tf)),
    )
