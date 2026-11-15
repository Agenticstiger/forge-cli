# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Typed errors for secret references.

Every message names the reference and never the value: a reference is a
pointer an operator may read in a log, a value is what the whole package
exists to keep out of one.
"""

from __future__ import annotations

from dataclasses import dataclass

from fluid_build._errors import FluidUserError, doc_url


@dataclass
class SecretError(FluidUserError):
    """Base class: catch this to handle every secret-reference refusal.

    ``str()`` carries the why and the fix as well as the what, so a caller that
    flattens the error into a one-line run failure still says how to fix it.
    None of the three ever holds a value. The what ends in a full stop, not a
    colon: the log redactor reads ``credential: A`` as an assignment and masks
    the word after it.
    """

    code: str = "SecretError"

    def __str__(self) -> str:
        return f"{self.what}. {self.why} Fix: {self.fix}"


@dataclass
class SecretRefError(SecretError):
    """A ``secretRef`` does not follow the grammar of its scheme."""

    code: str = "SecretRefError"


@dataclass
class SecretPolicyError(SecretError):
    """The operator's policy does not allow this reference here.

    Raised before any store is asked, so a refusal never reads the secret.
    """

    code: str = "SecretPolicyError"


@dataclass
class SecretResolveError(SecretError):
    """The store could not produce the value (unreachable, denied, missing)."""

    code: str = "SecretResolveError"


def resolve_failure(what: str, why: str, fix: str, reason: str) -> SecretResolveError:
    """The one factory for a store that could not produce a value.

    ``reason`` is the stable tag in ``extras``; ``what`` names the reference.
    """
    return SecretResolveError(
        what=what, why=why, fix=fix, doc=doc_url("secrets"), extras={"reason": reason}
    )
