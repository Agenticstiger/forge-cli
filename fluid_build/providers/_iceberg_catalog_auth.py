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

"""``sink.catalogAuth``: how a derived Iceberg sink authenticates to a REST catalog.

The contract names environment VARIABLES, never values. forge-cli emits each
secret as a catalog property whose value is ``env:<NAME>``, and Apache
Iceberg's REST client resolves such a value from the environment of the JVM
that builds the catalog: ``RESTSessionCatalog.initialize`` calls
``EnvironmentUtil.resolveAll`` on the client properties
(apache-iceberg-1.9.2 and apache-iceberg-1.10.0
core/src/main/java/org/apache/iceberg/rest/RESTSessionCatalog.java:195;
core/src/main/java/org/apache/iceberg/util/EnvironmentUtil.java:28, prefix
``"env:"``). The config forge-cli writes or pushes therefore holds the
variable's name; the value stays in the environment of the Kafka Connect worker
or the Debezium Server process. Nothing here reads the environment: only the
embedded Debezium preflight does, to test that the variable is set.

Upstream behaviour the emitted properties depend on (Iceberg core, same tags):

* An unset variable DROPS the property rather than failing
  (EnvironmentUtil.java:39-43). With no ``rest.auth.type``, a client whose
  ``credential`` was dropped infers auth type ``none``
  (rest/auth/AuthManagers.java:54-68), so ``rest.auth.type=oauth2`` is always
  emitted.
* ``credential`` is ``client_id:client_secret``, split on the first ``:``
  (rest/auth/OAuth2Util.java:73), so the id and the secret share one variable.
* With no ``oauth2-server-uri`` the client POSTs the credential to the
  catalog's own ``v1/oauth/tokens`` (rest/auth/AuthConfig.java:84-85), and
  with no ``scope`` it requests ``catalog`` (AuthConfig.java:83); both are
  therefore required for ``type: oauth2``.
* Given both ``token`` and ``credential``, the client uses the token
  (rest/auth/OAuth2Manager.java:118-119 at 1.10.0), so the validator refuses an
  override map that sets either next to ``catalogAuth``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

#: The ``properties.sink`` key this module reads.
CATALOG_AUTH_FIELD = "catalogAuth"

#: An environment variable NAME. A pasted secret with a character a name cannot
#: hold (``id:secret``, a JWT, a value with ``-`` or ``=``) does not match, so it
#: fails validation. The end anchor is ``(?![\s\S])``, not ``$``: Python's ``$``
#: (and the jsonschema library, which runs ``re.search``) also matches before a
#: trailing newline, and ``NAME\n`` would reach Iceberg as ``env:NAME\n``, a
#: variable no process has. The same text is the schema's ``pattern``, and a
#: lookahead reads the same in ECMA-262.
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?![\s\S])")

#: A NAME-shaped value that looks like a token: an ``_``-separated run of 20 or
#: more letters and digits that holds a digit. Personal access tokens are
#: often plain alphanumerics that pass :data:`ENV_NAME_RE` (a Databricks /
#: Unity ``dapi`` + 32 hex, a GitHub ``ghp_`` + 36, an AWS ``AKIA`` key id), and
#: a value pasted into ``tokenEnv`` would be emitted as ``env:<token>`` into the
#: config forge-cli pushes or writes. Variable names keep such runs short
#: (``LAKEKEEPER_CREDENTIAL``) or digit-free. A heuristic: a shorter token, or
#: one with no digit, still passes. The schema refuses the same text with ``not``.
SECRET_SHAPED_NAME_RE = re.compile(r"(?:^|_)(?=[A-Za-z]*[0-9])[A-Za-z0-9]{20,}(?:_|$)")

#: A ``${`` expression in a non-secret catalogAuth field. Debezium Server's
#: SmallRye config expands ``${...}`` in application.properties, while Kafka
#: Connect passes it on as literal text (its ConfigTransformer reads only
#: ``${provider:path:key}``), so the same contract would work on one engine
#: and fail on the other. The schema refuses the same text with ``not``.
EXPRESSION_RE = re.compile(r"\$\{")

#: An ASCII control character. A line break from a YAML block scalar inside a
#: scope is not a valid OAuth2 scope token (RFC 6749 section 3.3), and the
#: embedded Debezium runner refuses it in application.properties, so the
#: contract is refused before either engine sees it.
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

#: What the schema's ``not`` refuses in a text field: a ``${`` or a control
#: character (the parser reports them apart, with their own reasons).
FORBIDDEN_TEXT_PATTERN = f"{EXPRESSION_RE.pattern}|{CONTROL_RE.pattern}|^\\s*env:"

#: Iceberg's environment-reference prefix (EnvironmentUtil.java:28).
ENV_REF_PREFIX = "env:"

#: The catalog properties ``catalogAuth`` decides. An override map that sets
#: one of them next to ``catalogAuth`` is refused as ambiguous.
AUTH_OWNED_PROPERTIES: Tuple[str, ...] = (
    "rest.auth.type",
    "credential",
    "token",
    "oauth2-server-uri",
    "scope",
    "audience",
    "resource",
    # Iceberg's AuthManagers picks SigV4 over rest.auth.type when it is on.
    "rest.sigv4-enabled",
)

#: A static header the REST client sends with every request; Iceberg merges
#: the auth session's headers with putIfAbsent, so a configured one would win.
AUTHORIZATION_HEADER_PROPERTY = "header.authorization"

#: The NAMES of the catalog properties whose value is a secret (checked for
#: an inlined literal; the names themselves are not secret).
INLINE_CHECKED_KEYS: Tuple[str, ...] = ("credential", "token")

#: Per ``type``: the variable field, the other required fields, the optional ones.
_SHAPES: Mapping[str, Tuple[str, Tuple[str, ...], Tuple[str, ...]]] = {
    "oauth2": ("credentialEnv", ("oauth2ServerUri", "scope"), ("audience", "resource")),
    "bearer": ("tokenEnv", (), ()),
}

AUTH_TYPES: Tuple[str, ...] = tuple(_SHAPES)

#: The catalog properties whose value is a contract field, by that field.
_FIELD_OF_PROPERTY: Mapping[str, str] = {
    "oauth2-server-uri": "oauth2ServerUri",
    "scope": "scope",
    "audience": "audience",
    "resource": "resource",
}


@dataclass(frozen=True)
class CatalogAuth:
    """A parsed ``sink.catalogAuth``."""

    type: str
    #: The environment variable that holds the secret.
    env_var: str
    oauth2_server_uri: str = ""
    scope: str = ""
    audience: str = ""
    resource: str = ""

    @property
    def env_field(self) -> str:
        """``credentialEnv`` or ``tokenEnv``: the field that named :attr:`env_var`."""
        return _SHAPES[self.type][0]

    def catalog_properties(self) -> Dict[str, str]:
        """The Iceberg catalog properties, as BARE keys, in a fixed order.

        Kafka Connect takes them under ``iceberg.catalog.``; Debezium Server
        takes them bare (its runner adds ``debezium.sink.iceberg.``). A secret
        is an ``env:<NAME>`` reference, never a value, and never a KIP-887
        ``${env:...}`` or Quarkus ``${...}`` expression: Iceberg resolves
        ``env:`` itself, in the process that builds the catalog.
        """
        ref = f"{ENV_REF_PREFIX}{self.env_var}"
        props: Dict[str, str] = {"rest.auth.type": "oauth2"}
        if self.type == "bearer":
            # The static-token path of the oauth2 auth manager
            # (rest/auth/OAuth2Properties.java:25 ``token``).
            props["token"] = ref
            return props
        props["credential"] = ref
        props["oauth2-server-uri"] = self.oauth2_server_uri
        props["scope"] = self.scope
        # Sent on the token request only when set (OAuth2Util.java:142-151).
        if self.audience:
            props["audience"] = self.audience
        if self.resource:
            props["resource"] = self.resource
        return props

    def example_properties(self) -> Dict[str, str]:
        """:meth:`catalog_properties` for a message: each value the contract
        wrote (``oauth2-server-uri``, ``scope``, ...) as ``<field>``.

        A message shows only what forge-cli itself chose: ``rest.auth.type``
        and the ``env:<NAME>`` references. A contract value is not echoed into
        ``fluid validate`` output or a run error.
        """
        return {
            key: (f"<{_FIELD_OF_PROPERTY[key]}>" if key in _FIELD_OF_PROPERTY else value)
            for key, value in self.catalog_properties().items()
        }


def raw_catalog_auth(sink: Any) -> Any:
    """``sink.catalogAuth`` as written, or ``None``. ``sink`` is the raw mapping."""
    if not isinstance(sink, Mapping):
        return None
    return sink.get(CATALOG_AUTH_FIELD)


def parse_catalog_auth(raw: Any) -> Tuple[Optional[CatalogAuth], List[str]]:
    """Parse ``sink.catalogAuth``: ``(auth, [])``, ``(None, problems)`` or ``(None, [])``.

    ``(None, [])`` when ``raw`` is ``None`` (no catalogAuth). Each problem is a
    sentence naming the field. A problem never quotes a field's value: a value
    that fails the variable-name rule may be a pasted secret, and these
    messages reach ``fluid validate`` output and the run log.
    """
    if raw is None:
        return None, []
    where = f"sink.{CATALOG_AUTH_FIELD}"
    if not isinstance(raw, Mapping):
        return None, [f"{where} must be a mapping with a type ({' or '.join(AUTH_TYPES)})"]
    auth_type = raw.get("type")
    if not isinstance(auth_type, str) or auth_type not in _SHAPES:
        return None, [f"{where}.type must be one of: {', '.join(AUTH_TYPES)}"]
    env_field, required, optional = _SHAPES[str(auth_type)]
    allowed = {"type", env_field, *required, *optional}
    problems: List[str] = []
    unknown = sorted(str(k) for k in raw if k not in allowed)
    if unknown:
        problems.append(
            f"{where} of type {auth_type} does not take {unknown}; it takes "
            f"{sorted(allowed - {'type'})}"
        )
    env_var = raw.get(env_field)
    if not isinstance(env_var, str) or not env_var:
        problems.append(f"{where}.{env_field} is required for type {auth_type}")
    elif not ENV_NAME_RE.match(env_var):
        problems.append(
            f"{where}.{env_field} must NAME an environment variable (letters, digits and "
            "_, not starting with a digit, nothing after the name, not even a newline); the "
            "value is not shown, because it may be a pasted secret. Put the secret in that "
            "variable, not in the contract"
        )
    elif SECRET_SHAPED_NAME_RE.search(env_var):
        problems.append(
            f"{where}.{env_field} looks like a token, not an environment variable name: it "
            "holds a run of 20 or more letters and digits, with a digit, between "
            "underscores, the shape of a personal access token. The value is not shown. "
            "Put the token in an environment variable and name that variable here (a "
            "real name with such a run: split it with _)"
        )
    for key in required:
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{where}.{key} is required for type {auth_type}")
    for key in optional:
        if key in raw and (not isinstance(raw[key], str) or not raw[key].strip()):
            problems.append(f"{where}.{key} must be a non-empty string when set")
    for key in (*required, *optional):
        value = raw.get(key)
        if isinstance(value, str) and value.strip().startswith(ENV_REF_PREFIX):
            problems.append(
                f"{where}.{key} starts with {ENV_REF_PREFIX}, which Iceberg resolves from the "
                "environment (and drops when the variable is unset). Write the value itself; "
                f"only the secret comes from the environment, through {env_field}"
            )
        if isinstance(value, str) and CONTROL_RE.search(value):
            problems.append(
                f"{where}.{key} contains a control character, such as a line break or a "
                "tab; the value is not shown. Write it on one line"
            )
        if isinstance(value, str) and EXPRESSION_RE.search(value):
            problems.append(
                f"{where}.{key} holds a ${{...}} expression. Debezium Server expands one in "
                "application.properties and Kafka Connect sends it to Iceberg as literal "
                "text, so the sink would differ by engine. Write the value itself; only "
                f"the secret comes from the environment, through {env_field}"
            )
    uri = raw.get("oauth2ServerUri")
    if auth_type == "oauth2" and isinstance(uri, str) and uri.strip() and not _is_http_url(uri):
        problems.append(
            f"{where}.oauth2ServerUri must be an absolute http(s) URL of the identity "
            "provider's token endpoint, starting with a lowercase http:// or https:// "
            "(Iceberg treats any other spelling as a path on the catalog and sends the "
            "credentials there), with no user:password@ part (for Keycloak, "
            "<keycloak>/realms/<realm>/protocol/openid-connect/token); the value is not "
            "shown, because a user:password@ part may hold the client secret. The client "
            f"id and secret go in the variable {env_field} names"
        )
    if problems:
        return None, problems
    return (
        CatalogAuth(
            type=str(auth_type),
            env_var=str(env_var),
            oauth2_server_uri=str(raw.get("oauth2ServerUri") or "").strip(),
            scope=str(raw.get("scope") or "").strip(),
            audience=str(raw.get("audience") or "").strip(),
            resource=str(raw.get("resource") or "").strip(),
        ),
        [],
    )


def catalog_auth_of(sink: Any) -> Optional[CatalogAuth]:
    """The parsed ``sink.catalogAuth`` of a raw sink mapping, or ``None``.

    For a runner, after :func:`iceberg_sink_preflight` passed: a malformed
    catalogAuth is refused there, so this raises ``ValueError`` (naming the
    problems, never a value) only when a caller skipped the preflight.
    """
    auth, problems = parse_catalog_auth(raw_catalog_auth(sink))
    if problems:
        raise ValueError("; ".join(problems))
    return auth


def is_literal_secret(value: Any) -> bool:
    """Is ``value`` an inlined secret rather than a reference to one?

    ``False`` for an empty value, an Iceberg ``env:`` reference, and a value
    that carries a ``${...}`` expression (a Kafka Connect ConfigProvider or
    Quarkus property expression, which the runtime resolves).
    """
    text = str(value if value is not None else "").strip()
    return bool(text) and not text.startswith(ENV_REF_PREFIX) and "${" not in text


def _is_http_url(value: str) -> bool:
    """Is ``value`` an absolute ``http://`` / ``https://`` URL with a host and no userinfo?

    The scheme must be lowercase: Iceberg's ``HTTPRequest.hasAbsolutePath``
    tests ``startsWith("https://") || startsWith("http://")``
    (apache-iceberg-1.9.2, 1.10.0 and 1.11.0 HTTPRequest.java:138-140), so
    ``HTTPS://idp/...`` is resolved against the catalog uri and the client
    credentials go to the catalog host instead of the identity provider.

    A ``user:password@`` part is refused: it would carry the client secret,
    as plain text, into every config forge-cli writes or pushes and into
    plan.json, the leak ``credentialEnv`` exists to prevent.
    """
    text = value.strip()
    if any(ch.isspace() for ch in text):
        return False
    try:
        parts = urlsplit(text)
    except ValueError:
        return False
    if "@" in parts.netloc:
        return False
    return text.startswith(("http://", "https://")) and bool(parts.hostname)
