# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The :class:`RegistryKind` base and the kind registry (``kinds/__init__.py`` lists the
built-in kinds and how to add one)."""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Tuple

_ANY_SUBJECT = re.compile(r"^[^\x00-\x1f\x7f]{1,255}$")


class RegistryKind:
    """What one registry kind knows. Subclass, set the attributes, override the methods."""

    #: The ``kind`` value in a contract.
    name: str = ""
    #: How messages name the registry.
    title: str = ""
    #: The compatibility modes the registry accepts.
    modes: Tuple[str, ...] = ()
    #: A produced subject's mode when the contract names none.
    default_compatibility: str = ""
    #: The cloud whose OpenTofu plugin registers this kind's produced subjects.
    registering_cloud: str = ""
    #: The fix text for a produced subject no bound cloud would register.
    bind_hint: str = ""
    subject_pattern: "re.Pattern[str]" = _ANY_SUBJECT
    #: The spec fields this kind adds beyond kind, subject, version and compatibility.
    fields: FrozenSet[str] = frozenset()
    #: Other kinds' fields that :meth:`field_errors` refuses with its own message; any
    #: other kind's field gets the generic "is for kind: X" refusal (``reference``).
    refuses: FrozenSet[str] = frozenset()
    #: Whether the registry answers a dry-run compatibility check.
    checks_compatibility: bool = False

    # -- shape --------------------------------------------------------------

    def field_errors(self, spec: Mapping[str, Any], where: str) -> List[str]:
        """The kind-specific shape errors of a registry object."""
        return []

    def location(self, ref: Any) -> Optional[str]:
        """Where the registry is: what a label, a plan record and a message name."""
        return None

    def registry_block(self, ref: Any) -> Dict[str, Any]:
        """The ``stream.registry`` fields (beyond ``kind``) that point at ``ref``'s registry."""
        return {}

    # -- egress and operator checks -----------------------------------------

    def egress_findings(
        self, subject: Any, *, stage: str, environ: Optional[Mapping[str, str]] = None
    ) -> List[Tuple[str, str]]:
        """``(severity, message)`` for where a request for ``subject`` would go.

        Every kind states its own rules, even when it has none to state (Glue: IAM, no
        contract-named host): :func:`register_kind` refuses a kind that does not override
        this, so a new HTTP kind cannot silently skip the host and credential checks.
        """
        raise NotImplementedError(f"registry kind {self.name!r} states no egress rules")

    def produced_checks(
        self, subject: Any, *, stage: str, environ: Optional[Mapping[str, str]] = None
    ) -> Tuple[List[str], List[str]]:
        """``(errors, warnings)`` about the operator's environment for a produced subject."""
        return [], []

    def produced_key(self, ref: Any) -> str:
        """The name the emitter gives a produced subject: two refs with one key collide."""
        from fluid_build.iac.naming import safe_ident

        return safe_ident(str(ref.subject))

    def produced_errors(
        self, contract: Mapping[str, Any], subject: Any, expose: Mapping[str, Any]
    ) -> List[str]:
        """Kind-specific errors across a produced subject and its expose."""
        return []

    # -- reading ------------------------------------------------------------

    def client(
        self,
        ref: Any,
        *,
        contract: Optional[Mapping[str, Any]] = None,
        environ: Optional[Mapping[str, str]] = None,
        credential: Optional[str] = None,
        factory: Optional[Callable[..., Any]] = None,
    ) -> Any:
        """The read client for ``ref``'s registry; ``factory`` replaces the real one."""
        if factory is None:
            raise NotImplementedError(f"registry kind {self.name!r} has no client")
        return factory(ref)

    def read(self, client: Any, ref: Any) -> Any:
        """``ref``'s subject version, as a :class:`~..clients.SubjectVersion`."""
        raise NotImplementedError(f"registry kind {self.name!r} cannot be read")

    def mode(self, client: Any, ref: Any) -> Optional[str]:
        """The subject's compatibility mode (only when ``checks_compatibility``)."""
        raise NotImplementedError(f"registry kind {self.name!r} has no compatibility check")

    def verdict(
        self, client: Any, ref: Any, version: Any, schema_text: str, data_format: str
    ) -> Tuple[bool, List[str]]:
        """The registry's compatibility answer (only when ``checks_compatibility``)."""
        raise NotImplementedError(f"registry kind {self.name!r} has no compatibility check")


_KINDS: Dict[str, RegistryKind] = {}


def register_kind(kind: RegistryKind) -> RegistryKind:
    """Add ``kind``. A name already registered is refused, and so is a kind that states
    no egress rules (fail closed: see :meth:`RegistryKind.egress_findings`)."""
    if kind.name in _KINDS:
        raise ValueError(f"registry kind {kind.name!r} is already registered")
    if type(kind).egress_findings is RegistryKind.egress_findings:
        raise TypeError(
            f"registry kind {kind.name!r} states no egress rules: override egress_findings "
            "(return [] only for a kind that sends nothing to a contract-named host)"
        )
    _KINDS[kind.name] = kind
    return kind


def get_kind(name: Any) -> RegistryKind:
    """The kind called ``name``; ``ValueError`` naming the known kinds otherwise."""
    found = _KINDS.get(name) if isinstance(name, str) else None
    if found is None:
        raise ValueError(f"unknown registry kind {name!r}; known: {', '.join(_KINDS)}")
    return found


def names() -> Tuple[str, ...]:
    """The registered kind names, in registration order."""
    return tuple(_KINDS)


def all_kinds() -> Tuple[RegistryKind, ...]:
    """The registered kinds, in registration order."""
    return tuple(_KINDS.values())
