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

"""``exposes[].policy.privacy.masking[]`` applied to the rows before they land.

Every bundled schema (fluid-schema 0.7.1 to 0.7.6) accepts
``{column, strategy: mask|hash|tokenize|encrypt|k_anonymity, params}`` on an
expose, and until this module only the MCP output-port gateway read it (it
drops every masked column from what it serves). The DuckDB acquisition runner
landed the column in cleartext, so a contract could declare ``msisdn`` hashed
and ship the phone numbers to S3.

The runner now rewrites each masked column inside the ``COPY`` statement, so
what reaches the destination (a local file, an S3 object, the file a BigQuery
load job reads, and the DLQ) is already treated. Quality gates still run on the
source values, before the rewrite. A treated column always lands as a string,
so the contract must declare it with a string type (``string``, or ``VARCHAR``
where ``schemaPolicy`` compares the source's own type names).

Strategies, and what a landed value looks like (``fluid verify`` checks every
non-null value of a masked column against these shapes):

``hash``
    Lowercase hex SHA-256 of ``salt || value`` (both UTF-8), so the same value
    always hashes the same and joins across products that share the salt still
    work. The salt comes from the environment variable ``params.saltEnv``
    names, default ``FLUID_PII_HASH_SECRET``, and must be at least 16 bytes. An
    unsalted SHA-256 of a phone number is reversed by enumerating the numbering
    plan, so an unset salt refuses the build rather than hashing without one.
    Shape: 64 characters ``[0-9a-f]``. The same digest in SQL:
    ``lower(to_hex(sha256(to_utf8(salt || value))))`` on Athena,
    ``sha256(salt || value)`` on DuckDB.
``mask``
    Every character except the first ``params.keepFirst`` (default 0) and the
    last ``params.keepLast`` (default 4) replaced by ``*``, length preserved:
    ``+46701234567`` becomes ``********4567``. A value no longer than
    ``keepFirst + keepLast`` is masked entirely. No secret. Shape: the kept
    characters around at least one ``*``, or ``*`` only.
``tokenize``
    HMAC-SHA256 of the value keyed by the environment variable
    ``params.keyEnv`` names, default ``FLUID_PII_TOKENIZATION_KEY``, truncated
    to 32 lowercase hex characters: the token ``tokenize_pii`` (the ``preLand``
    hook) already produces, from the same variable, so both paths agree.
    Deterministic for one key. The key must be at least 32 bytes (RFC 2104
    section 3: a key shorter than the hash output is "strongly discouraged").
    Unset, the build is refused; the hook's ephemeral-key fallback is not
    reused, because tokens nobody can reproduce are not what a declared policy
    asks for. Shape: 32 characters ``[0-9a-f]``.
``encrypt``
    AES-GCM with a random 96-bit nonce and the column name as associated data,
    written as ``aesgcm:v1:`` followed by unpadded base64url of
    ``nonce || ciphertext || tag``. The key is the base64 of 16, 24 or 32 bytes
    in the environment variable ``params.keyEnv`` names, default
    ``FLUID_PII_ENCRYPTION_SECRET_KEY`` (``openssl rand -base64 32``). Reversible with
    the key (:func:`decrypt_value`), not deterministic, so joins on it do not
    work. With random 96-bit nonces NIST SP 800-38D caps one key at 2^32
    encryptions, so rotate it well before that many values have been written.
    Shape: ``aesgcm:v1:`` then at least 38 characters ``[A-Za-z0-9_-]``.
``k_anonymity``
    Refused (:class:`MaskingStrategyUnsupported`). k-anonymity is a property of
    a whole table over a set of quasi-identifiers, not something one value can
    be turned into: Google's Sensitive Data Protection computes it as a risk
    analysis job over a table's quasi-identifiers, not as a transformation. A
    per-column rule landed batch by batch cannot guarantee it, and landing the
    column untreated would be the cleartext this module exists to prevent.

Secrets are read from the environment only, never from the contract, so they
cannot reach the Glue table's ``fluid_contract`` parameter, the plan or argv.
They live in the closures of the DuckDB UDFs that apply them, never in SQL
text (DuckDB echoes a failing statement into its error, which the runner
records), and every value read is registered with the exact-value log redactor.
A literal ``salt`` or ``key`` in ``params`` is refused, for the same reason.

Borrowed, not built:

* The strategy set and the parameter shapes follow Microsoft Presidio's
  anonymizer operators (``presidio_anonymizer/operators``): ``hash`` appends
  its salt to the text and hex-digests SHA-256, and refuses a salt under 16
  bytes; ``mask`` replaces a count of characters with one masking character.
  Presidio's ``hash`` falls back to a random salt when none is given, which
  makes it non-deterministic; here the salt is required, because a hash that
  cannot join is not what ``hash`` promises.
* The keep-first/keep-last partial mask is SQL Server Dynamic Data Masking's
  ``partial(prefix, padding, suffix)`` and PostgreSQL Anonymizer's
  ``anon.partial``; unlike ``partial`` the length is preserved, so the masked
  column still says how long the value was, and a too-short value is masked
  whole rather than partly exposed.
* PostgreSQL Anonymizer's ``anon.digest(value, salt, algorithm)`` takes its
  salt from a secret setting (``anon.salt``) and says the salt must be
  protected like the data itself; hence the environment variable.
* ``tokenize`` is ``_hmac_token`` from ``build_runners/hooks/tokenize_pii.py``
  (repeated, see :func:`tokenize_value`).
* ``encrypt`` diverges from Presidio, whose ``encrypt`` is AES-CBC without
  authentication: AES-GCM through ``cryptography``'s ``AESGCM`` (already a core
  dependency) authenticates the ciphertext, and a 96-bit random nonce is the
  length its documentation recommends.
* The MCP gateway (``output_ports/mcp/drivers/base.py``) has no strategy
  definitions to reuse: it drops every column a masking rule names.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from fluid_build.providers._sql_safety import validate_ident

LOG = logging.getLogger("fluid.acquire.masking")

HASH = "hash"
MASK = "mask"
TOKENIZE = "tokenize"
ENCRYPT = "encrypt"
K_ANONYMITY = "k_anonymity"

#: Every strategy the contract schema's enum accepts (fluid-schema 0.7.1 to 0.7.6).
SCHEMA_STRATEGIES: Tuple[str, ...] = (MASK, HASH, TOKENIZE, ENCRYPT, K_ANONYMITY)
#: The strategies a value can be put through at landing.
LANDING_STRATEGIES: Tuple[str, ...] = (HASH, MASK, TOKENIZE, ENCRYPT)

# The default variable names are credential-shaped on purpose: the repo's own
# redactor (``secret_redactor.is_sensitive_key_name``) then treats them as
# secrets, so ``fluid apply`` leaves a ``{{ env.FLUID_PII_HASH_SECRET }}``
# written into a contract literal instead of resolving the salt into
# ``main.tf.json`` and the Glue table's ``fluid_contract`` parameter
# (``cli/_common.py::resolve_env_templates_in_contract``), and the CLI registers
# ``*_SECRET`` / ``*_SECRET_KEY`` values for exact-value log redaction at start.
# ``FLUID_PII_HASH_SALT`` would have been neither. A variable a rule names
# itself should follow the same rule; its value is registered here either way.
DEFAULT_SALT_ENV = "FLUID_PII_HASH_SECRET"
#: The variable the ``tokenize_pii`` preLand hook already reads
#: (``hooks/tokenize_pii.py::_TOKEN_KEY_ENV``; a test pins that they agree).
DEFAULT_TOKENIZATION_KEY_ENV = "FLUID_PII_TOKENIZATION_KEY"
#: ``hooks/tokenize_pii.py::_TOKEN_HEX_LEN``: 128 bits of the HMAC.
TOKEN_HEX_LEN = 32
DEFAULT_ENCRYPTION_KEY_ENV = "FLUID_PII_ENCRYPTION_SECRET_KEY"

DEFAULT_KEEP_FIRST = 0
DEFAULT_KEEP_LAST = 4
#: Upper bound for keepFirst / keepLast: a mask that keeps more is not a mask,
#: and it keeps the shape regex's repetition counts small.
MAX_KEEP = 64
MASK_CHAR = "*"

#: Presidio's floor for a hash salt.
MIN_SALT_BYTES = 16
#: RFC 2104 section 3: less than L bytes (32 for SHA-256) is strongly discouraged.
MIN_TOKENIZATION_KEY_BYTES = 32
#: AES-128, AES-192, AES-256.
ENCRYPTION_KEY_BYTES: Tuple[int, ...] = (16, 24, 32)
ENCRYPTED_PREFIX = "aesgcm:v1:"
_NONCE_BYTES = 12
_TAG_BYTES = 16

# Upper case only, and a value that is not one is never echoed: a salt pasted
# into ``saltEnv`` by mistake (``openssl rand -hex`` output is lower case,
# base64 has lower case and ``+/=``) is refused without being repeated into
# the run record, instead of being looked up and named as a missing variable.
_ENV_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]{0,127}")

_SECRET_PARAM = {HASH: "saltEnv", TOKENIZE: "keyEnv", ENCRYPT: "keyEnv"}
_DEFAULT_SECRET_ENV = {
    HASH: DEFAULT_SALT_ENV,
    TOKENIZE: DEFAULT_TOKENIZATION_KEY_ENV,
    ENCRYPT: DEFAULT_ENCRYPTION_KEY_ENV,
}
_ALLOWED_PARAMS: Dict[str, Tuple[str, ...]] = {
    HASH: ("saltEnv",),
    MASK: ("keepFirst", "keepLast"),
    TOKENIZE: ("keyEnv",),
    ENCRYPT: ("keyEnv",),
}
#: A param with one of these names carries a secret in the contract itself.
_LITERAL_SECRET_PARAMS = ("salt", "key", "secret", "hmacKey", "encryptionKey", "password")

#: Declared column types a treated (string) column may carry. Compared on the
#: lower-cased base name, so ``VARCHAR(64)`` and ``character varying`` pass.
_STRING_TYPES = frozenset(
    {
        "string",
        "str",
        "text",
        "varchar",
        "char",
        "character",
        "character varying",
        "nvarchar",
        "nchar",
        "bpchar",
        "utf8",
    }
)

_UDF_PREFIX = "__fluid_mask_"


# ── Errors ──────────────────────────────────────────────────────────────


class MaskingPolicyError(ValueError):
    """A masking rule the build cannot apply, so it refuses to land anything.

    Messages name columns, strategies and environment variable NAMES, never a
    secret's value or a row's value, because the runner records them.
    """

    code = "masking_policy_invalid"

    def __init__(
        self, message: str, *, column: Optional[str] = None, strategy: Optional[str] = None
    ) -> None:
        super().__init__(message)
        self.column = column
        self.strategy = strategy


class MaskingStrategyUnsupported(MaskingPolicyError):
    """A schema-valid strategy this runner cannot apply honestly (``k_anonymity``)."""

    code = "masking_strategy_unsupported"


class MaskingSecretMissing(MaskingPolicyError):
    """The salt or key variable is unset, empty, too short or not decodable."""

    code = "masking_secret_missing"


class MaskingColumnMissing(MaskingPolicyError):
    """A rule names a column the landed data does not have."""

    code = "masking_column_missing"


class MaskingTypeIncompatible(MaskingPolicyError):
    """The contract declares a masked column with a type a treated value cannot have."""

    code = "masking_type_incompatible"


# ── Rules ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MaskingRule:
    """One ``policy.privacy.masking[]`` entry, validated."""

    column: str
    strategy: str
    keep_first: int = DEFAULT_KEEP_FIRST
    keep_last: int = DEFAULT_KEEP_LAST
    #: The environment variable holding the salt or key; ``None`` for ``mask``.
    secret_env: Optional[str] = None

    @property
    def appliable(self) -> bool:
        return self.strategy in LANDING_STRATEGIES

    @property
    def shape(self) -> Optional[str]:
        """The regex a treated value fully matches; ``None`` when there is none.

        Unanchored and written in the subset RE2 (DuckDB), Joni and RE2J
        (Athena) and Python's ``re`` share: callers full-match it
        (``regexp_full_match``, ``\\A(?:...)\\z``, ``re.fullmatch``).
        ``[\\s\\S]`` rather than ``.``, which stops at a newline in all three.
        """
        if self.strategy == HASH:
            return "[0-9a-f]{64}"
        if self.strategy == TOKENIZE:
            return f"[0-9a-f]{{{TOKEN_HEX_LEN}}}"
        if self.strategy == ENCRYPT:
            # 12-byte nonce + 16-byte tag at least: 28 bytes, 38 base64url chars.
            # The prefix holds no regex metacharacter.
            return ENCRYPTED_PREFIX + "[A-Za-z0-9_-]{38,}"
        if self.strategy == MASK:
            head = f"[\\s\\S]{{{self.keep_first}}}" if self.keep_first else ""
            tail = f"[\\s\\S]{{{self.keep_last}}}" if self.keep_last else ""
            return f"{head}\\*+{tail}|\\**"
        return None

    @property
    def shape_description(self) -> str:
        if self.strategy == HASH:
            return "64 lowercase hexadecimal characters (SHA-256)"
        if self.strategy == TOKENIZE:
            return "32 lowercase hexadecimal characters (HMAC-SHA256 token)"
        if self.strategy == ENCRYPT:
            return f"'{ENCRYPTED_PREFIX}' followed by base64url (AES-GCM)"
        if self.strategy == MASK:
            kept = [
                f"the first {self.keep_first}" if self.keep_first else "",
                f"the last {self.keep_last}" if self.keep_last else "",
            ]
            but = " and ".join(k for k in kept if k)
            return f"'{MASK_CHAR}' in place of every character" + (f" but {but}" if but else "")
        return f"no per-value shape: {self.strategy} is not applied to values"

    def matches_shape(self, value: Any) -> bool:
        """Whether one landed value has this rule's shape. ``None`` always does."""
        if value is None:
            return True
        shape = self.shape
        return shape is not None and re.fullmatch(shape, str(value)) is not None

    def describe(self) -> Dict[str, Any]:
        """What the run record says about this rule: never a secret, only its variable."""
        out: Dict[str, Any] = {"strategy": self.strategy}
        if self.strategy == MASK:
            out["keepFirst"] = self.keep_first
            out["keepLast"] = self.keep_last
        if self.secret_env:
            out[_SECRET_PARAM[self.strategy]] = self.secret_env
        return out


def _expose_label(expose: Mapping[str, Any]) -> str:
    return str(expose.get("exposeId") or expose.get("id") or "?")


def _int_param(
    params: Mapping[str, Any], name: str, default: int, where: str, column: str, strategy: str
) -> int:
    value = params.get(name, default)
    # bool is an int subclass; ``keepLast: true`` is a typo, not a 1.
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_KEEP:
        raise MaskingPolicyError(
            f"{where}: params.{name} for column {column!r} must be a whole number from 0 to "
            f"{MAX_KEEP}, got {value!r}",
            column=column,
            strategy=strategy,
        )
    return value


def _parse_rule(raw: Any, where: str, *, allow_unsupported: bool) -> MaskingRule:
    if not isinstance(raw, Mapping):
        raise MaskingPolicyError(f"{where}: a masking rule must be a mapping, got {raw!r}")
    column = raw.get("column")
    strategy = raw.get("strategy")
    if not isinstance(column, str) or not column:
        raise MaskingPolicyError(f"{where}: a masking rule needs a column name")
    try:
        validate_ident(column)
    except ValueError:
        raise MaskingPolicyError(
            f"{where}: masking column {column!r} is not a plain identifier "
            "(letters, digits and underscores, not starting with a digit)",
            column=column,
        ) from None
    if strategy not in SCHEMA_STRATEGIES:
        raise MaskingPolicyError(
            f"{where}: column {column!r} has masking strategy {strategy!r}; "
            f"the schema allows {', '.join(SCHEMA_STRATEGIES)}",
            column=column,
        )
    if strategy == K_ANONYMITY:
        if allow_unsupported:
            return MaskingRule(column=column, strategy=strategy)
        raise MaskingStrategyUnsupported(
            f"{where}: masking strategy 'k_anonymity' on column {column!r} is not applied at "
            "landing, so the build refuses to run rather than land the column untreated. "
            "k-anonymity is a property of a whole table over a set of quasi-identifiers, "
            "which a per-column rule applied batch by batch cannot guarantee. Use hash, "
            "tokenize, mask or encrypt for this column, or generalise the quasi-identifiers "
            "in a transformation build.",
            column=column,
            strategy=strategy,
        )

    params = raw.get("params") or {}
    if not isinstance(params, Mapping):
        raise MaskingPolicyError(
            f"{where}: params for column {column!r} must be a mapping, got {params!r}",
            column=column,
            strategy=strategy,
        )
    literal = sorted(k for k in params if str(k) in _LITERAL_SECRET_PARAMS)
    if literal:
        raise MaskingPolicyError(
            f"{where}: column {column!r} carries a literal {literal[0]!r} in params. A salt "
            "or key written into the contract is published with it (plan, bundle, the Glue "
            f"table's fluid_contract parameter); name an environment variable in "
            f"params.{_SECRET_PARAM.get(strategy, 'keyEnv')} instead.",
            column=column,
            strategy=strategy,
        )
    allowed = _ALLOWED_PARAMS[strategy]
    unknown = sorted(str(k) for k in params if k not in allowed)
    if unknown:
        raise MaskingPolicyError(
            f"{where}: column {column!r} ({strategy}) has unknown params "
            f"{', '.join(unknown)}; {strategy} takes {', '.join(allowed)}",
            column=column,
            strategy=strategy,
        )

    if strategy == MASK:
        return MaskingRule(
            column=column,
            strategy=strategy,
            keep_first=_int_param(params, "keepFirst", DEFAULT_KEEP_FIRST, where, column, strategy),
            keep_last=_int_param(params, "keepLast", DEFAULT_KEEP_LAST, where, column, strategy),
        )
    param = _SECRET_PARAM[strategy]
    env_name = params.get(param, _DEFAULT_SECRET_ENV[strategy])
    if not isinstance(env_name, str) or not _ENV_NAME_RE.fullmatch(env_name):
        raise MaskingPolicyError(
            f"{where}: params.{param} for column {column!r} must be the NAME of an "
            "environment variable in upper case ([A-Z_][A-Z0-9_]*), not a salt or key; its "
            "value is not repeated here",
            column=column,
            strategy=strategy,
        )
    return MaskingRule(column=column, strategy=strategy, secret_env=env_name)


def masking_rules(
    expose: Optional[Mapping[str, Any]], *, allow_unsupported: bool = False
) -> List[MaskingRule]:
    """The expose's masking rules, validated; ``[]`` when it declares none.

    ``allow_unsupported`` returns a ``k_anonymity`` rule instead of refusing it,
    for ``fluid verify``, which reports it rather than applying it. Raises
    :class:`MaskingPolicyError` for a rule the build cannot apply as written.
    """
    if not isinstance(expose, Mapping):
        return []
    policy = expose.get("policy") or {}
    privacy = policy.get("privacy") if isinstance(policy, Mapping) else None
    raw_rules = privacy.get("masking") if isinstance(privacy, Mapping) else None
    if not raw_rules:
        return []
    where = f"exposes[{_expose_label(expose)}].policy.privacy.masking"
    if not isinstance(raw_rules, list):
        raise MaskingPolicyError(f"{where} must be a list of rules")
    rules: List[MaskingRule] = []
    seen: Dict[str, str] = {}
    for raw in raw_rules:
        rule = _parse_rule(raw, where, allow_unsupported=allow_unsupported)
        key = rule.column.lower()
        if key in seen:
            raise MaskingPolicyError(
                f"{where}: column {rule.column!r} has two masking rules ({seen[key]} and "
                f"{rule.strategy}); one column lands one way",
                column=rule.column,
                strategy=rule.strategy,
            )
        seen[key] = rule.strategy
        rules.append(rule)
    return rules


def _declared_schema(expose: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    section = expose.get("contract")
    raw = (section.get("schema") if isinstance(section, Mapping) else None) or expose.get("schema")
    if isinstance(raw, Mapping):
        raw = raw.get("fields")
    if not isinstance(raw, list):
        return []
    return [c for c in raw if isinstance(c, Mapping) and c.get("name")]


def is_string_type(declared: Any) -> bool:
    """Whether a declared column type can hold a treated value (always a string)."""
    base = re.sub(r"\s*\(.*\)\s*$", "", str(declared or "").strip().lower())
    return base in _STRING_TYPES


def check_declared_types(expose: Mapping[str, Any], rules: Sequence[MaskingRule]) -> None:
    """Refuse a masked column the contract declares with a non-string type.

    A hashed ``customer_id INTEGER`` lands as 64 hex characters: the Glue table
    would declare ``int`` over a string column and Athena would refuse to read
    it. A column the schema does not declare, or declares without a type, is
    not checked here; the landed column still has to exist.
    """
    by_name = {str(c["name"]).lower(): c for c in _declared_schema(expose)}
    for rule in rules:
        if not rule.appliable:
            continue
        declared = by_name.get(rule.column.lower())
        if declared is None or not declared.get("type"):
            continue
        if not is_string_type(declared.get("type")):
            raise MaskingTypeIncompatible(
                f"exposes[{_expose_label(expose)}].contract.schema declares {rule.column!r} as "
                f"{declared.get('type')}, but {rule.strategy} lands it as a string "
                f"({rule.shape_description}). Declare it with a string type: string, or "
                "VARCHAR where schemaPolicy compares the source's own type names.",
                column=rule.column,
                strategy=rule.strategy,
            )


# ── Value functions ─────────────────────────────────────────────────────


def mask_value(
    value: str, *, keep_first: int = DEFAULT_KEEP_FIRST, keep_last: int = DEFAULT_KEEP_LAST
) -> str:
    """``mask``: keep the first and last characters asked for, ``*`` the rest."""
    length = len(value)
    if length <= keep_first + keep_last:
        return MASK_CHAR * length
    hidden = MASK_CHAR * (length - keep_first - keep_last)
    return value[:keep_first] + hidden + value[length - keep_last :]


def hash_value(value: str, *, salt: bytes) -> str:
    """``hash``: lowercase hex SHA-256 of ``salt || value``."""
    return hashlib.sha256(salt + value.encode("utf-8")).hexdigest()


def tokenize_value(value: str, *, key: bytes) -> str:
    """``tokenize``: the ``tokenize_pii`` hook's HMAC-SHA256 token.

    The hook's ``_hmac_token``, repeated rather than imported: importing
    ``build_runners.hooks`` builds its ``REGISTRY``, whose ``TokenizePiiHook()``
    warns about an ephemeral key on every build that merely masks a column.
    ``tests/build_runners/test_masking.py`` pins the two to the same token.
    """
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()[:TOKEN_HEX_LEN]


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def encrypt_value(value: str, *, key: bytes, column: str) -> str:
    """``encrypt``: ``aesgcm:v1:`` + base64url(nonce || ciphertext || tag)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(_NONCE_BYTES)
    sealed = AESGCM(key).encrypt(nonce, value.encode("utf-8"), column.encode("utf-8"))
    return ENCRYPTED_PREFIX + _b64url(nonce + sealed)


def decrypt_value(token: str, *, key: bytes, column: str) -> str:
    """Reverse :func:`encrypt_value`. Raises ``ValueError`` for a malformed token
    and ``cryptography.exceptions.InvalidTag`` for a wrong key or column."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not token.startswith(ENCRYPTED_PREFIX):
        raise ValueError(f"not an {ENCRYPTED_PREFIX} value")
    body = token[len(ENCRYPTED_PREFIX) :]
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except (binascii.Error, ValueError) as exc:
        raise ValueError("the encrypted value is not base64url") from exc
    if len(raw) < _NONCE_BYTES + _TAG_BYTES:
        raise ValueError("the encrypted value is too short to hold a nonce and a tag")
    nonce, sealed = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
    # bytes(...): the strict CI gate type-checks without cryptography installed.
    plain = bytes(AESGCM(key).decrypt(nonce, sealed, column.encode("utf-8")))
    return plain.decode("utf-8")


def _decode_encryption_key(text: str) -> Optional[bytes]:
    compact = "".join(text.split())
    try:
        raw = base64.b64decode(
            compact.replace("-", "+").replace("_", "/") + "=" * (-len(compact) % 4),
            validate=True,
        )
    except (binascii.Error, ValueError):
        return None
    return raw if len(raw) in ENCRYPTION_KEY_BYTES else None


def _read_secret(rule: MaskingRule, environ: Mapping[str, str]) -> bytes:
    """The salt or key ``rule`` names, as bytes. Never puts the value in an error."""
    env_name = rule.secret_env or ""
    text = environ.get(env_name, "")
    what = "salt" if rule.strategy == HASH else "key"
    if not text:
        raise MaskingSecretMissing(
            f"masking {rule.strategy} on column {rule.column!r} needs a {what} in the "
            f"environment variable {env_name}, which is unset or empty. Refusing to land the "
            f"column untreated: set {env_name} in the build's environment (a CI credential, "
            "never the contract).",
            column=rule.column,
            strategy=rule.strategy,
        )
    if rule.strategy == ENCRYPT:
        key = _decode_encryption_key(text)
        if key is None:
            raise MaskingSecretMissing(
                f"masking encrypt on column {rule.column!r}: {env_name} must hold the base64 "
                f"of a {', '.join(str(n) for n in ENCRYPTION_KEY_BYTES)}-byte AES key "
                "(openssl rand -base64 32); its value is not that.",
                column=rule.column,
                strategy=rule.strategy,
            )
    else:
        key = text.encode("utf-8")
        floor = MIN_SALT_BYTES if rule.strategy == HASH else MIN_TOKENIZATION_KEY_BYTES
        if len(key) < floor:
            raise MaskingSecretMissing(
                f"masking {rule.strategy} on column {rule.column!r}: the {what} in {env_name} "
                f"is shorter than the {floor} bytes required "
                "(openssl rand -hex 32 makes one).",
                column=rule.column,
                strategy=rule.strategy,
            )
    # Exact-value log redaction for the rest of the process, so a value that
    # reaches a log line by any route is masked by value.
    from fluid_build.observability.secret_redactor import register_secret

    register_secret(text)
    return key


def _value_function(rule: MaskingRule, secret: Optional[bytes]) -> Callable[[str], str]:
    if rule.strategy == MASK:
        first, last = rule.keep_first, rule.keep_last
        return lambda value: mask_value(value, keep_first=first, keep_last=last)
    if secret is None:  # pragma: no cover - _read_secret raised already
        raise MaskingSecretMissing(f"no secret resolved for {rule.column!r}")
    if rule.strategy == HASH:
        return lambda value: hash_value(value, salt=secret)
    if rule.strategy == TOKENIZE:
        return lambda value: tokenize_value(value, key=secret)
    column = rule.column
    return lambda value: encrypt_value(value, key=secret, column=column)


# ── Landing ─────────────────────────────────────────────────────────────


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


@dataclass
class LandingMasker:
    """The masking a build applies: rules plus the functions that apply them.

    The secrets live only inside ``_functions``' closures, and neither the
    dataclass ``repr`` nor :meth:`facet` shows them.
    """

    expose_id: str
    rules: Tuple[MaskingRule, ...]
    _functions: Dict[str, Callable[[str], str]] = field(default_factory=dict, repr=False)

    @classmethod
    def for_expose(
        cls,
        expose: Optional[Mapping[str, Any]],
        *,
        cursor_field: Optional[str] = None,
        environ: Optional[Mapping[str, str]] = None,
    ) -> Optional["LandingMasker"]:
        """Everything that can be refused before a byte moves. ``None``: no rules.

        ``cursor_field`` is the incremental cursor, which cannot be masked: the
        next run filters the source on the landed maximum.
        """
        rules = masking_rules(expose)
        if not rules or expose is None:
            return None
        check_declared_types(expose, rules)
        if cursor_field:
            for rule in rules:
                if rule.column.lower() == cursor_field.lower():
                    raise MaskingPolicyError(
                        f"column {rule.column!r} is the incremental cursor_field; masking it "
                        "would make the next run filter the source on a treated value. Mask "
                        "another column or use another cursor.",
                        column=rule.column,
                        strategy=rule.strategy,
                    )
        env = os.environ if environ is None else environ
        functions: Dict[str, Callable[[str], str]] = {}
        for rule in rules:
            secret = _read_secret(rule, env) if rule.secret_env else None
            functions[rule.column] = _value_function(rule, secret)
        return cls(expose_id=_expose_label(expose), rules=tuple(rules), _functions=functions)

    def _udf_name(self, index: int) -> str:
        return f"{_UDF_PREFIX}{index}"

    def install(self, con: Any) -> None:
        """Register one scalar UDF per rule on ``con`` (VARCHAR in, VARCHAR out).

        DuckDB's default null handling passes NULL through untouched. ``encrypt``
        is registered with ``side_effects`` so DuckDB never folds two calls
        into one: each value gets its own nonce.
        """
        for index, rule in enumerate(self.rules):
            con.create_function(
                self._udf_name(index),
                self._functions[rule.column],
                ["VARCHAR"],
                "VARCHAR",
                side_effects=rule.strategy == ENCRYPT,
            )

    def landed_columns(self, con: Any, select_sql: str, stream: str) -> Dict[str, str]:
        """Each rule's column as the stream spells it; refuses a missing one."""
        described = con.execute(f"DESCRIBE {select_sql}").fetchall()
        names = {str(row[0]).lower(): str(row[0]) for row in described}
        resolved: Dict[str, str] = {}
        for rule in self.rules:
            actual = names.get(rule.column.lower())
            if actual is None:
                raise MaskingColumnMissing(
                    f"exposes[{self.expose_id}].policy.privacy.masking names column "
                    f"{rule.column!r}, which stream {stream!r} does not have (it has "
                    f"{', '.join(sorted(names.values())) or 'no columns'}). Fix the rule or the "
                    "source; nothing was landed.",
                    column=rule.column,
                    strategy=rule.strategy,
                )
            resolved[rule.column] = actual
        return resolved

    def projection(self, select_sql: str, columns: Mapping[str, str]) -> str:
        """``select_sql`` with every masked column replaced by its treated value."""
        replaced = []
        for index, rule in enumerate(self.rules):
            ident = _quote_ident(columns.get(rule.column, rule.column))
            replaced.append(f"{self._udf_name(index)}(CAST({ident} AS VARCHAR)) AS {ident}")
        return f"SELECT * REPLACE ({', '.join(replaced)}) FROM ({select_sql})"

    def facet(self) -> Dict[str, Any]:
        """``facets.masking`` of the run record."""
        return {
            "applied": "at_landing",
            "expose": self.expose_id,
            "columns": {rule.column: rule.describe() for rule in self.rules},
        }


def masked_column_types(expose: Optional[Mapping[str, Any]]) -> Dict[str, str]:
    """``{column (lower-cased): "VARCHAR"}`` for the columns a build would treat.

    For the schema fingerprint: the declared schema describes what LANDS, so a
    masked column is compared as the string it becomes. An invalid rule yields
    nothing here; the build refuses it on its own.
    """
    try:
        rules = masking_rules(expose)
    except MaskingPolicyError:
        return {}
    return {rule.column.lower(): "VARCHAR" for rule in rules if rule.appliable}
