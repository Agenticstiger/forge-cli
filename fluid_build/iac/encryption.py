# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""OpenTofu state and plan encryption for ``fluid apply``: the operator names a key, never a value.

Opt-in (founder decision, 2026-11-15). The operator sets ``FLUID_STATE_ENCRYPTION``
in the real environment (a protected name: a project's dotenv files cannot set
it) to one OpenTofu key provider and its key:

* ``aws-kms://<key id, key ARN or alias/<name>>`` (cloud): ``aws_kms``. The region
  comes from a key ARN, else from ``AWS_REGION`` / ``AWS_DEFAULT_REGION``.
* ``gcp-kms://projects/<p>/locations/<l>/keyRings/<r>/cryptoKeys/<k>`` (cloud): ``gcp_kms``.
* ``openbao://<transit mount path>/<key name>`` (on site): ``openbao``. ``tofu``
  gets the store's address and a token in its environment only (``BAO_ADDR``,
  ``BAO_TOKEN``): the token forge's vault resolver finds or obtains, the
  operator's ``BAO_TOKEN`` / ``VAULT_TOKEN``, a response-wrapped token or a CI
  JWT login (``secrets/_vault.py``), revoked when the run ends when forge minted it.
* ``pbkdf2+<secret reference>`` (the lab): ``pbkdf2``, keyed by the passphrase the
  reference names (``pbkdf2+env://FLUID_STATE_PASSPHRASE``,
  ``pbkdf2+vault://kv/lab/state#passphrase``), resolved under the operator's
  secret policy. It reaches ``tofu`` as an ephemeral variable whose value is
  ``TF_VAR_fluid_state_passphrase`` in ``tofu``'s environment only: the module
  holds ``${var.fluid_state_passphrase}``, OpenTofu keeps only an ephemeral
  variable's name in a saved plan, and never puts it in the state.

The module then carries one ``terraform.encryption`` block: the key provider,
one ``aes_gcm`` method, and ``state`` and ``plan`` both ``enforced = true``, so
``tofu`` refuses to write either unencrypted. Nothing is written to
``TF_ENCRYPTION``, and an operator's ``TF_ENCRYPTION`` never reaches ``tofu``
(``iac/credentials.py``): the environment would override the module.

State written before encryption was switched on is plaintext, which an enforced
configuration refuses to read. ``fluid apply --migrate-state-encryption`` reads
it once through OpenTofu's ``unencrypted`` fallback and writes it back
encrypted. ``enforced`` forbids an ``unencrypted`` method, so that one run's
state is not enforced; its plan still is.

Requires OpenTofu 1.11 (ephemeral variables). Borrowed:
opentofu.org/docs/language/state/encryption/ and the OpenTofu 1.11 ephemeral
values. Measured on OpenTofu 1.13.1 with ``.tf.json``: a ``method`` reference
is a bare traversal string (``"method.aes_gcm.fluid"``; ``"${...}"`` is refused
there) while ``keys`` takes ``"${key_provider.<type>.fluid}"``; an ephemeral
variable may key ``pbkdf2``; ``tofu apply <plan>`` needs it set again;
``enforced`` plus an ``unencrypted`` fallback fails "Unencrypted method is
forbidden".
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

from .naming import tofu_ref

#: The operator's setting (``credentials/protected_env.py``).
STATE_ENCRYPTION_ENV = "FLUID_STATE_ENCRYPTION"
#: The ephemeral variable that keys ``pbkdf2``.
PASSPHRASE_VARIABLE = "fluid_state_passphrase"
#: Ephemeral variables arrived in OpenTofu 1.11.
MIN_TOFU_VERSION: Tuple[int, int, int] = (1, 11, 0)
#: OpenTofu's minimum for a ``pbkdf2`` passphrase.
MIN_PASSPHRASE_LENGTH = 16

_NAME = "fluid"
_MIGRATE = "fluid_migrate"
_PBKDF2_PREFIX = "pbkdf2+"
_FORMS = (
    "aws-kms://<key id, key ARN or alias/<name>>",
    "gcp-kms://projects/<p>/locations/<l>/keyRings/<r>/cryptoKeys/<k>",
    "openbao://<transit mount path>/<key name>",
    "pbkdf2+<secret reference>, e.g. pbkdf2+env://FLUID_STATE_PASSPHRASE",
)

# Locators are written into main.tf.json, so none may hold "$", "%", "{" or a
# control character (OpenTofu interpolation).
_AWS_KEY_ARN = re.compile(
    r"arn:aws[a-z-]*:kms:(?P<region>[a-z]{2,}(?:-[a-z]+)+-\d+):\d{12}:(?:key|alias)/[A-Za-z0-9/_+=,.@-]+"
)
_AWS_KEY_OTHER = re.compile(r"alias/[A-Za-z0-9/_-]+|(?:mrk-)?[0-9a-fA-F-]{32,40}")
_GCP_KEY = re.compile(
    r"projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/locations/[a-z0-9-]+/keyRings/[A-Za-z0-9_-]+"
    r"/cryptoKeys/[A-Za-z0-9_-]+"
)
_BAO_SEGMENT = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*")
#: Regions of every partition: us-east-1, us-gov-west-1, cn-north-1, eusc-de-east-1.
_AWS_REGION = re.compile(r"[a-z]{2,}(?:-[a-z]+)+-\d+")


class StateEncryptionError(ValueError):
    """``FLUID_STATE_ENCRYPTION`` or the state it applies to cannot be used.

    ``event`` is the run's error event, ``kind`` a stable tag, ``remediation``
    the next steps (the shape of ``iac.base.UnsupportedBindingError``).
    """

    def __init__(self, event: str, kind: str, message: str, remediation: Tuple[str, ...] = ()):
        super().__init__(message)
        self.event = event
        self.kind = kind
        self.remediation = remediation


@dataclass(frozen=True)
class StateEncryption:
    """The key provider ``FLUID_STATE_ENCRYPTION`` names. Holds no secret."""

    provider: str  # aws_kms | gcp_kms | openbao | pbkdf2
    key: str = ""
    region: Optional[str] = None
    transit_path: Optional[str] = None
    passphrase_ref: Optional[str] = None

    @property
    def clouds(self) -> Tuple[str, ...]:
        """The cloud whose variables ``tofu`` needs to reach the key."""
        return {"aws_kms": ("aws",), "gcp_kms": ("gcp",)}.get(self.provider, ())

    def describe(self) -> str:
        """What the apply prints: the provider and the key's name, never key material."""
        if self.provider == "pbkdf2":
            return f"pbkdf2, passphrase from {self.passphrase_ref}"
        if self.provider == "openbao":
            return f"openbao transit key {self.key} at {self.transit_path}"
        return f"{self.provider} {self.key}"


def _invalid(why: str) -> StateEncryptionError:
    return StateEncryptionError(
        "opentofu_state_encryption_invalid",
        "invalid",
        f"{STATE_ENCRYPTION_ENV} cannot be used: {why}",
        (f"Set {STATE_ENCRYPTION_ENV} to one of: " + "; ".join(_FORMS),),
    )


def parse_state_encryption(
    spec: str, environ: Optional[Mapping[str, str]] = None
) -> StateEncryption:
    """Read a ``FLUID_STATE_ENCRYPTION`` value; raise :class:`StateEncryptionError` if unusable."""
    text = spec.strip()
    if text.startswith(_PBKDF2_PREFIX):
        return _pbkdf2(text[len(_PBKDF2_PREFIX) :])
    scheme, sep, locator = text.partition("://")
    if not sep or not locator:
        raise _invalid("it names no key provider.")
    if scheme == "aws-kms":
        return _aws_kms(locator, os.environ if environ is None else environ)
    if scheme == "gcp-kms":
        if not _GCP_KEY.fullmatch(locator):
            raise _invalid(
                "a gcp-kms key is projects/<p>/locations/<l>/keyRings/<r>/cryptoKeys/<k>."
            )
        return StateEncryption("gcp_kms", key=locator)
    if scheme == "openbao":
        segments = locator.split("/")
        if len(segments) < 2 or not all(
            _BAO_SEGMENT.fullmatch(s) and s not in (".", "..") for s in segments
        ):
            raise _invalid("an openbao key is <transit mount path>/<key name>, e.g. transit/fluid.")
        return StateEncryption(
            "openbao", key=segments[-1], transit_path="/" + "/".join(segments[:-1])
        )
    raise _invalid(f"{scheme!r} is not a key provider forge supports.")


def _pbkdf2(raw: str) -> StateEncryption:
    from fluid_build.secrets.errors import SecretError
    from fluid_build.secrets.ref import parse_secret_ref

    try:
        ref = parse_secret_ref(raw)
    except SecretError as exc:
        raise _invalid(f"the passphrase reference is not valid ({exc}).") from None
    return StateEncryption("pbkdf2", passphrase_ref=ref.canonical)


def _aws_kms(locator: str, environ: Mapping[str, str]) -> StateEncryption:
    arn = _AWS_KEY_ARN.fullmatch(locator)
    if arn is not None:
        return StateEncryption("aws_kms", key=locator, region=arn.group("region"))
    if not _AWS_KEY_OTHER.fullmatch(locator):
        raise _invalid("an aws-kms key is a key id, a key ARN or alias/<name>.")
    region = (environ.get("AWS_REGION") or environ.get("AWS_DEFAULT_REGION") or "").strip()
    if not _AWS_REGION.fullmatch(region):
        raise _invalid(
            f"aws-kms://{locator} names no region: give the key's ARN, or set AWS_REGION."
        )
    return StateEncryption("aws_kms", key=locator, region=region)


def configured_state_encryption(
    environ: Optional[Mapping[str, str]] = None,
) -> Optional[StateEncryption]:
    """The key provider the operator configured, or ``None`` (state left unencrypted)."""
    source = os.environ if environ is None else environ
    raw = (source.get(STATE_ENCRYPTION_ENV) or "").strip()
    return parse_state_encryption(raw, source) if raw else None


def _key_provider(encryption: StateEncryption) -> Dict[str, Any]:
    if encryption.provider == "aws_kms":
        body: Dict[str, Any] = {"kms_key_id": encryption.key, "key_spec": "AES_256"}
        if encryption.region:
            body["region"] = encryption.region
        return body
    if encryption.provider == "gcp_kms":
        return {"kms_encryption_key": encryption.key, "key_length": 32}
    if encryption.provider == "openbao":
        return {"key_name": encryption.key, "transit_engine_path": encryption.transit_path}
    return {"passphrase": tofu_ref(f"var.{PASSPHRASE_VARIABLE}")}


def encryption_block(encryption: StateEncryption, *, migrate: bool = False) -> Dict[str, Any]:
    """The ``terraform.encryption`` block; ``migrate`` reads plaintext state for one run."""
    method = f"method.aes_gcm.{_NAME}"
    methods: Dict[str, Any] = {
        "aes_gcm": {_NAME: {"keys": tofu_ref(f"key_provider.{encryption.provider}.{_NAME}")}}
    }
    state: Dict[str, Any] = {"method": method, "enforced": True}
    if migrate:
        methods["unencrypted"] = {_MIGRATE: {}}
        state = {"method": method, "fallback": {"method": f"method.unencrypted.{_MIGRATE}"}}
    return {
        "key_provider": {encryption.provider: {_NAME: _key_provider(encryption)}},
        "method": methods,
        "state": state,
        "plan": {"method": method, "enforced": True},
    }


def encryption_variables(encryption: StateEncryption) -> Dict[str, Any]:
    """The module's ``variable`` blocks the key needs: the ephemeral passphrase, for ``pbkdf2``."""
    if encryption.provider != "pbkdf2":
        return {}
    return {PASSPHRASE_VARIABLE: {"type": "string", "sensitive": True, "ephemeral": True}}


def require_supported_tofu(version: Optional[Tuple[int, ...]]) -> None:
    """Refuse a ``tofu`` older than 1.11 (``version`` from ``runner.tofu_version()``)."""
    if version is None or tuple(version) >= MIN_TOFU_VERSION:
        return
    found = ".".join(str(p) for p in version)
    raise StateEncryptionError(
        "opentofu_state_encryption_unsupported",
        "tofu-too-old",
        f"{STATE_ENCRYPTION_ENV} needs OpenTofu 1.11 or newer (ephemeral variables keep "
        f"the key out of the saved plan); this tofu is {found}.",
        (
            "Upgrade OpenTofu to 1.11 or newer (https://opentofu.org/docs/intro/install/). "
            "--ensure-opentofu installs a pinned build only when no tofu is on PATH",
        ),
    )


def encryption_env(encryption: StateEncryption) -> Dict[str, str]:
    """What ``tofu``'s environment needs to reach the key, fetched now, for this run only.

    ``pbkdf2``: the passphrase (``TF_VAR_fluid_state_passphrase``), resolved
    through the operator's secret policy and registered with the redactor.
    ``openbao``: the store's address and a token. The KMS providers read the
    cloud's own credentials, which the allowlist passes. Raises
    :class:`StateEncryptionError`; its message names references, never values.
    """
    from fluid_build.secrets.errors import SecretError

    try:
        if encryption.provider == "pbkdf2":
            return {f"TF_VAR_{PASSPHRASE_VARIABLE}": _passphrase(encryption)}
        if encryption.provider == "openbao":
            return _openbao_env()
    except SecretError as exc:
        raise StateEncryptionError(
            "opentofu_state_encryption_unavailable",
            "key-unavailable",
            f"the state encryption key ({encryption.describe()}) could not be reached: {exc}",
            (str(getattr(exc, "fix", "") or "Check FLUID_STATE_ENCRYPTION and the store"),),
        ) from None
    return {}


def _passphrase(encryption: StateEncryption) -> str:
    from fluid_build.secrets.resolve import resolve_credential

    value = resolve_credential(str(encryption.passphrase_ref)).get_secret_value()
    if len(value) < MIN_PASSPHRASE_LENGTH:
        raise StateEncryptionError(
            "opentofu_state_encryption_unavailable",
            "passphrase-too-short",
            f"the passphrase {encryption.passphrase_ref} names is shorter than "
            f"{MIN_PASSPHRASE_LENGTH} characters, OpenTofu's minimum for pbkdf2.",
            ("Store a longer passphrase (a password manager's 32 random characters).",),
        )
    return value


def _openbao_env() -> Dict[str, str]:
    # The vault resolver's own config and token, not resolve_credential: no
    # secret value is read here, only the token tofu sends to the transit engine
    # the operator named. The address (BAO_ADDR / VAULT_ADDR) and the CA bundle
    # are protected names, so a project file cannot redirect the token, and a
    # token the resolver minted is revoked when the run ends.
    from fluid_build.secrets import _vault
    from fluid_build.secrets.run import state_for_lookup

    config = _vault.load_config(None)
    with state_for_lookup() as run:
        token = _vault.token_for(None, config, run)
    env = {"BAO_ADDR": config.address, "BAO_TOKEN": token}
    if config.namespace:
        env["BAO_NAMESPACE"] = config.namespace
    if config.ca_bundle:
        env["BAO_CACERT"] = config.ca_bundle
    return env


#: ``tofu``'s messages for state it cannot read under the encryption it was
#: given (measured on OpenTofu 1.13.1), matched with whitespace collapsed.
_FAILURES = (
    (
        "encountered unencrypted payload",
        "opentofu_state_not_encrypted",
        "state-not-encrypted",
        f"the state is not encrypted, and {STATE_ENCRYPTION_ENV} asks for encrypted state.",
        (
            "Run `fluid apply` once with --migrate-state-encryption: that run reads the "
            "plaintext state and writes it back encrypted",
            f"Or unset {STATE_ENCRYPTION_ENV} to keep this state unencrypted",
        ),
    ),
    (
        "can not be read without an encryption configuration",
        "opentofu_state_encrypted_without_key",
        "state-encrypted",
        f"the state is encrypted, and {STATE_ENCRYPTION_ENV} is not set.",
        (f"Set {STATE_ENCRYPTION_ENV} to the key that encrypted it",),
    ),
    (
        "decryption failed for all provided methods",
        "opentofu_state_decrypt_failed",
        "key-mismatch",
        f"the state or plan could not be decrypted with the key {STATE_ENCRYPTION_ENV} names.",
        (
            f"Set {STATE_ENCRYPTION_ENV} to the key that encrypted this state; forge does "
            "not change keys yet (that needs the old key as a fallback)",
        ),
    ),
)


#: ``tofu``'s summaries when the key provider itself fails (the key cannot be
#: reached or used: no credentials, a denied key, an unreachable store).
_KEY_UNREACHABLE = ("Unable to build encryption key data", "Unable to fetch encryption key data")
_REASON = re.compile(r"key_provider\.[a-z_]+\.fluid failed with error: (.{1,300}?)(?:\\n|\"|$)")


def explain_state_failure(output: str) -> Optional[StateEncryptionError]:
    """The fix-it for a ``tofu`` run that failed on state encryption, else ``None``."""
    flat = " ".join((output or "").split())
    for needle, event, kind, message, remediation in _FAILURES:
        if needle in flat:
            return StateEncryptionError(event, kind, message, remediation)
    if any(needle in flat for needle in _KEY_UNREACHABLE):
        reason = _REASON.search(flat)
        return StateEncryptionError(
            "opentofu_state_encryption_unavailable",
            "key-unreachable",
            "tofu could not use the state encryption key "
            + (f"({reason.group(1).strip()})" if reason else "that FLUID_STATE_ENCRYPTION names")
            + ".",
            (
                f"Check that this identity may use the key {STATE_ENCRYPTION_ENV} names: the "
                "cloud credentials for aws-kms / gcp-kms, BAO_ADDR and the token for openbao",
                "Re-run with --debug to see tofu's full diagnostic",
            ),
        )
    return None


__all__ = [
    "MIN_TOFU_VERSION",
    "PASSPHRASE_VARIABLE",
    "STATE_ENCRYPTION_ENV",
    "StateEncryption",
    "StateEncryptionError",
    "configured_state_encryption",
    "encryption_block",
    "encryption_env",
    "encryption_variables",
    "explain_state_failure",
    "parse_state_encryption",
    "require_supported_tofu",
]
