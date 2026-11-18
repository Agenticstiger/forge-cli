# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""OAuth2 client-secret and bearer-JWT shapes, through BOTH redaction layers.

forge-cli runs two parallel redaction layers that must stay symmetric (CLAUDE.md):

* the global ``redact_secret_text`` / ``redact_value`` / ``SecretRedactingFilter`` in
  ``fluid_build/observability/secret_redactor.py``;
* the Snowflake-provider-local ``redact_string`` / ``redact_dict`` in
  ``fluid_build/providers/snowflake/util/logging.py``.

A target that signs in with an OAuth2 client-credentials grant, or with a personal JWT sent
as ``Authorization: Bearer``, brings four environment variables: a client id, a client
secret, a host name and a token. Only the secret and the token are credentials. This module
feeds each way of writing them through each layer and pins three things:

* the secret and the token are masked wherever they are written: an env-style assignment,
  JSON text, a Python ``repr``, a URL query, a form body, a ``Bearer`` header, free text;
* the client id and the host name are left alone;
* ``register_secrets_from_environ`` holds the secret and the token by value, and only those.

The JWT shape is ``redact_jose_tokens``, one implementation both layers call (adapted from
gitleaks' ``jwt`` rule, MIT, and detect-secrets' ``JwtTokenDetector``, Apache-2.0; nothing is
copied here). Fake values are built at runtime from parts, so no token-shaped literal sits
in the source (GitHub push protection, detect-secrets).
"""

from __future__ import annotations

import base64
import io
import json
import logging
from collections.abc import Callable, Iterator

import pytest

from fluid_build.observability.secret_redactor import (
    forget_known_secrets,
    install_secret_redacting_filter,
    known_secret_count,
    redact_secret_text,
    redact_value,
    register_secrets_from_environ,
)
from fluid_build.providers.snowflake.util.logging import redact_dict, redact_string

pytestmark = [pytest.mark.unit]


_JWT = ".".join(
    base64.urlsafe_b64encode(part).rstrip(b"=").decode("ascii")
    for part in (
        json.dumps({"alg": "RS256", "typ": "JWT", "kid": "k-1"}).encode(),
        json.dumps({"sub": "user-1", "iss": "https://idp.example", "exp": 1900000000}).encode(),
        bytes(range(7, 71)),
    )
)
# No JOSE shape and no delimiter: only the key name or the held value can mask it.
_SECRET = "-".join(("Zk4q", "R9tW", "vL2x", "Pn8m"))
_CLIENT_ID = "-".join(("0a1b2c3d", "4e5f", "6789", "abcd", "ef0123456789"))
# A long first label is the shape a JWT-looking-run rule must leave alone.
_HOSTNAME = "acme-prod-stack.example.com"

_LAYERS: dict[str, Callable[[str], str]] = {
    "global": redact_secret_text,
    "snowflake": redact_string,
}


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    """The exact-value registry is process-wide; keep every test here hermetic."""
    forget_known_secrets()
    yield
    forget_known_secrets()


@pytest.fixture(params=sorted(_LAYERS))
def redact(request: pytest.FixtureRequest) -> Callable[[str], str]:
    """Each test below runs once per layer."""
    return _LAYERS[request.param]


def _leaked(output: str, value: str) -> bool:
    """True when the start, the middle or the end of ``value`` survives in ``output``.

    A value cut short at a delimiter leaves its tail behind, so the whole string is not
    the only thing to look for.
    """
    middle = len(value) // 2
    return any(part in output for part in (value[:8], value[middle : middle + 8], value[-8:]))


_VALUES = {"opaque": _SECRET, "jwt": _JWT}

# Values that hold a delimiter an unquoted-value rule cuts at: a space, ``;``, ``&`` or ``,``.
_AWKWARD_VALUES = {
    "space": "Zk4q R9tW vL2x Pn8m",
    "semicolon": "Zk4q;R9tW;vL2x;Pn8m",
    "ampersand": "Zk4q&R9tW&vL2x&Pn8m",
    "comma": "Zk4q,R9tW,vL2x,Pn8m",
}
# What an Azure SAS token looks like: it always holds ``&``, and ``sig=`` is the HMAC.
_SAS_TOKEN = "&".join(
    ("sv=2021-08-06", "ss=b", "srt=sco", "sp=rl", "se=2026-12-01", f"sig={_SECRET}")
)
# A user-delegation SAS runs to ~300 characters or more, and the last one is past the 2,048 that
# the quoted-value rule takes, so only a key's own JSON pattern can mask it whole.
_LONG_SAS_TOKEN = "&".join((_SAS_TOKEN, *(f"p{n}={_SECRET}" for n in range(16))))
_HUGE_SAS_TOKEN = "&".join((_SAS_TOKEN, *(f"p{n}={_SECRET}" for n in range(160))))

# Every way the same ``name`` and ``value`` get written into a log line, an error body or a
# request dump. ``{{`` and ``}}`` are literal braces for ``str.format``.
_ASSIGNMENT_SHAPES = {
    "bare": "{name}={value}",
    "spaced": "{name} = {value}",
    "export": "export {name}={value}",
    "colon": "{name}: {value}",
    "mid-line": "loaded {name}={value} next=1",
    "double-quoted value": '{name}="{value}"',
    "json": '{{"{name}": "{value}"}}',
    "json spaced key": '{{ "{name}" : "{value}" }}',
    "json pretty": '{{\n  "{name}": "{value}",\n  "other": 1\n}}',
    "python repr": "{{'{name}': '{value}'}}",
    "url query": "https://host.example/oauth2/token?{name}={value}&x=1",
    "form body": "grant_type=client_credentials&client_id=abc-123&{name}={value}",
}


class TestCredentialAssignments:
    """``PLTR_CLIENT_SECRET`` / ``PLTR_TOKEN`` / ``client_secret`` carry a credential."""

    @pytest.mark.parametrize("name", ["PLTR_CLIENT_SECRET", "PLTR_TOKEN", "client_secret"])
    @pytest.mark.parametrize("kind", sorted(_VALUES))
    @pytest.mark.parametrize("shape", sorted(_ASSIGNMENT_SHAPES))
    def test_value_is_masked(
        self, redact: Callable[[str], str], name: str, kind: str, shape: str
    ) -> None:
        value = _VALUES[kind]
        line = _ASSIGNMENT_SHAPES[shape].format(name=name, value=value)
        out = redact(line)
        assert not _leaked(out, value), f"{kind} value leaked from {shape!r}: {out!r}"

    @pytest.mark.parametrize(
        "name",
        [
            "session_token",
            "id_token",
            "oauth_token",
            "password",
            "api_key",
            "secret",
            "token",
            "private_key",
        ],
    )
    @pytest.mark.parametrize("shape", ["json", "json spaced key", "python repr"])
    def test_other_credential_names_in_quoted_forms(
        self, redact: Callable[[str], str], name: str, shape: str
    ) -> None:
        # The Snowflake layer used to cover a quoted key only for the names it listed as a
        # JSON pattern (``"password":``), and not for ``'password':`` or any other name.
        line = _ASSIGNMENT_SHAPES[shape].format(name=name, value=_SECRET)
        assert not _leaked(redact(line), _SECRET)

    @pytest.mark.parametrize("name", ["PLTR_TOKEN", "client_secret", "password"])
    @pytest.mark.parametrize(
        "shape", ["double-quoted value", "json", "json spaced key", "python repr"]
    )
    @pytest.mark.parametrize("kind", sorted(_AWKWARD_VALUES))
    def test_quoted_value_holding_a_delimiter_is_masked_whole(
        self, redact: Callable[[str], str], name: str, shape: str, kind: str
    ) -> None:
        value = _AWKWARD_VALUES[kind]
        out = redact(_ASSIGNMENT_SHAPES[shape].format(name=name, value=value))
        assert not _leaked(out, value), f"{kind} value cut short in {shape!r}: {out!r}"

    def test_form_body_keeps_the_non_secret_fields(self, redact: Callable[[str], str]) -> None:
        line = _ASSIGNMENT_SHAPES["form body"].format(name="client_secret", value=_SECRET)
        out = redact(line)
        assert out.startswith("grant_type=client_credentials&client_id=abc-123&client_secret=")
        assert not _leaked(out, _SECRET)


_STAGE_KEYS = [
    "azure_sas_token",
    "aws_secret_key",
    "aws_secret_access_key",
    "AWS_SECRET_ACCESS_KEY",
]
_STAGE_SHAPES = ["json", "json pretty", "json spaced key", "python repr"]


class TestExternalStageKeys:
    """``azure_sas_token`` and ``aws_secret_*`` have their own JSON pattern in the Snowflake
    layer and also match its generic assignment pattern. The whole value has to go whichever
    pattern gets there first: a SAS token holds ``&``, and a value cut at it leaves ``sig=``."""

    @pytest.mark.parametrize("name", _STAGE_KEYS)
    @pytest.mark.parametrize("shape", _STAGE_SHAPES)
    def test_a_sas_token_is_masked_whole(
        self, redact: Callable[[str], str], name: str, shape: str
    ) -> None:
        out = redact(_ASSIGNMENT_SHAPES[shape].format(name=name, value=_SAS_TOKEN))
        assert not _leaked(out, _SAS_TOKEN), f"SAS token cut short in {shape!r}: {out!r}"

    @pytest.mark.parametrize("name", [*_STAGE_KEYS, "client_secret", "PLTR_TOKEN"])
    @pytest.mark.parametrize("shape", _STAGE_SHAPES)
    def test_a_long_sas_token_is_masked_whole_in_the_snowflake_layer(
        self, name: str, shape: str
    ) -> None:
        # ~440 characters. The global layer's quoted-value rule stops at 256 and cuts this
        # one, on ``main`` as well; that layer is not asserted here.
        out = redact_string(_ASSIGNMENT_SHAPES[shape].format(name=name, value=_LONG_SAS_TOKEN))
        assert not _leaked(out, _LONG_SAS_TOKEN), f"long SAS token cut short in {shape!r}: {out!r}"

    @pytest.mark.parametrize("name", _STAGE_KEYS)
    @pytest.mark.parametrize("shape", ["json", "json pretty"])
    def test_a_value_past_the_quoted_bound_is_masked_whole_by_the_keys_own_pattern(
        self, name: str, shape: str
    ) -> None:
        # ~3,700 characters: the generic rule would cut it, so the key's own JSON pattern has
        # to run first.
        out = redact_string(_ASSIGNMENT_SHAPES[shape].format(name=name, value=_HUGE_SAS_TOKEN))
        assert not _leaked(out, _HUGE_SAS_TOKEN), f"huge SAS token cut short in {shape!r}"


# Built at runtime, so no private-key header sits in the source (detect-secrets).
_PEM_BODY = base64.b64encode(b"synthetic key body that must vanish").decode("ascii")
_PEM = f"-----BEGIN {'PRIVATE KEY'}-----\n{_PEM_BODY}\n-----END {'PRIVATE KEY'}-----"

_PEM_UNDER_A_KEY = {
    "bare secret": "secret={pem}",  # pragma: allowlist secret
    "bare token": "token: {pem}",
    "bare password": "password={pem}",  # pragma: allowlist secret
    "json": '{{"token": "{pem}"}}',
    "json spaced key": '{{ "secret" : "{pem}" }}',
    "python repr client_secret": "{{'client_secret': '{pem}'}}",
    "python repr secret": "{{'secret': '{pem}'}}",
    "python repr private_key": "{{'private_key': '{pem}'}}",
    "bare private_key": "private_key={pem}",  # pragma: allowlist secret
}


class TestPemBlockUnderACredentialKey:
    """A key block is masked whole, whatever key carries it.

    The Snowflake layer used to run its ``key=value`` patterns before its PEM-block
    pattern, so a ``secret=`` or ``'client_secret': '`` took the ``-----BEGIN`` token for
    the value and left the key body behind, no longer a PEM block.
    """

    @pytest.mark.parametrize("shape", sorted(_PEM_UNDER_A_KEY))
    def test_key_body_is_masked(self, redact: Callable[[str], str], shape: str) -> None:
        out = redact(_PEM_UNDER_A_KEY[shape].format(pem=_PEM))
        assert not _leaked(out, _PEM_BODY), f"key body leaked from {shape!r}: {out!r}"


# A JWT, however a request dump, a redirect or a log line carries it.
_JWT_SHAPES = {
    "bearer header": "Authorization: Bearer {jwt}",
    "bearer header lower": "authorization: bearer {jwt}",
    "bearer bare": "sent Bearer {jwt} to host",
    "bearer in json": '{{"Authorization": "Bearer {jwt}"}}',
    "bearer in python repr": "{{'Authorization': 'Bearer {jwt}'}}",
    "bearer in http dump": "> GET /api HTTP/1.1\n> Authorization: Bearer {jwt}\n> Host: h",
    "bearer in curl": 'curl -H "Authorization: Bearer {jwt}" https://host.example/api',
    "free text": "issued {jwt} for user",
    "line start": "{jwt} was issued",
    "line end": "issued {jwt}",
    "double quotes": 'token "{jwt}" issued',
    "single quotes": "token '{jwt}' issued",
    "parentheses": "(token {jwt})",
    "trailing dot": "token {jwt}.",
    "trailing comma": "token {jwt}, next",
    "multi-line": "line1\ntoken\n{jwt}\nline4",
    "url query": "https://host.example/api?token={jwt}&x=1",
    "url query, other key": "https://host.example/api?k={jwt}",
    "url query, id_token": "https://host.example/api?a=1&id_token={jwt}&b=2",
    "url query, encoded =": "https://host.example/api?token%3D{jwt}",
    "url fragment": "https://host.example/cb#access_token={jwt}&state=1",
    "url path": "https://host.example/v1/{jwt}/items",
    "url inside json": '{{"url": "https://host.example/api?t={jwt}"}}',
}


class TestBearerJwt:
    @pytest.mark.parametrize("shape", sorted(_JWT_SHAPES))
    def test_jwt_is_masked(self, redact: Callable[[str], str], shape: str) -> None:
        out = redact(_JWT_SHAPES[shape].format(jwt=_JWT))
        assert not _leaked(out, _JWT), f"JWT leaked from {shape!r}: {out!r}"

    def test_jwt_is_masked_in_a_nested_value(self) -> None:
        payload = {"detail": f"issued {_JWT} for user", "list": [f"t={_JWT}"], "n": 1}
        for out in (redact_value(payload), redact_dict(payload)):
            assert not _leaked(json.dumps(out), _JWT)
            assert out["n"] == 1


class TestNonSecretsSurvive:
    """A client id and a host name are not credentials; masking them blinds the logs."""

    @pytest.mark.parametrize(
        "line",
        [
            f"PLTR_CLIENT_ID={_CLIENT_ID} PLTR_HOSTNAME={_HOSTNAME}",
            f'{{"PLTR_CLIENT_ID": "{_CLIENT_ID}", "PLTR_HOSTNAME": "{_HOSTNAME}"}}',
            f"connecting to https://{_HOSTNAME}/oauth2/token as {_CLIENT_ID}",
        ],
        ids=["env", "json", "prose"],
    )
    def test_text_is_unchanged(self, redact: Callable[[str], str], line: str) -> None:
        assert redact(line) == line

    def test_dict_keys_are_unchanged(self) -> None:
        payload = {"PLTR_CLIENT_ID": _CLIENT_ID, "PLTR_HOSTNAME": _HOSTNAME}
        assert redact_value(payload) == payload
        assert redact_dict(payload) == payload


class TestDictForms:
    @pytest.mark.parametrize(
        "key", ["client_secret", "PLTR_CLIENT_SECRET", "PLTR_TOKEN", "Authorization"]
    )
    def test_credential_key_is_masked_at_any_depth(self, key: str) -> None:
        payload = {key: _SECRET, "nested": {key: _SECRET}, "items": [{key: _SECRET}], "ok": "kept"}
        for out in (redact_value(payload), redact_dict(payload)):
            assert _SECRET not in json.dumps(out)
            assert out["ok"] == "kept"


class TestEnvironRegistration:
    """``register_secrets_from_environ`` holds the secret and the token, by value."""

    _ENV = {
        "PLTR_CLIENT_ID": _CLIENT_ID,
        "PLTR_CLIENT_SECRET": _SECRET,
        "PLTR_HOSTNAME": _HOSTNAME,
        "PLTR_TOKEN": _JWT,
    }

    def test_only_the_secret_and_the_token_are_held(self) -> None:
        assert register_secrets_from_environ(self._ENV) == 2
        assert known_secret_count() == 2

    def test_held_values_are_masked_and_the_rest_is_kept(
        self, redact: Callable[[str], str]
    ) -> None:
        register_secrets_from_environ(self._ENV)
        line = f"id={_CLIENT_ID} host={_HOSTNAME} s=<{_SECRET}> t=x{_JWT}y"
        out = redact(line)
        assert _CLIENT_ID in out
        assert _HOSTNAME in out
        assert not _leaked(out, _SECRET)
        assert not _leaked(out, _JWT)

    def test_a_held_secret_with_delimiters_is_masked_whole(
        self, redact: Callable[[str], str]
    ) -> None:
        # The pattern layer must guess where an unquoted value ends and truncates at a
        # ``;``, a space or a ``,`` inside it. The held literal needs no guess.
        awkward = "Zk4q;R9tW vL2x,Pn8m}"
        assert register_secrets_from_environ({"PLTR_CLIENT_SECRET": awkward}) == 1
        out = redact(f"login failed PLTR_CLIENT_SECRET={awkward} for host {_HOSTNAME}")
        assert "R9tW" not in out
        assert "Pn8m" not in out
        assert _HOSTNAME in out


class TestLoggingPipeline:
    """The global layer, wired into a real logger the way ``fluid`` installs it."""

    @pytest.fixture
    def captured(self) -> Iterator[tuple[logging.Logger, io.StringIO]]:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger = logging.Logger("fluid.test.redaction.shapes", level=logging.DEBUG)
        logger.addHandler(handler)
        install_secret_redacting_filter(logger)
        yield logger, stream
        logger.removeHandler(handler)

    def test_credential_shapes_never_reach_the_stream(
        self, captured: tuple[logging.Logger, io.StringIO]
    ) -> None:
        logger, stream = captured
        logger.debug("PLTR_TOKEN=%s", _JWT)
        logger.debug("exchanging client_secret=%s for a token", _SECRET)
        logger.debug("headers=%s", {"Authorization": f"Bearer {_JWT}"})
        logger.debug("granted %s", _JWT)
        logger.debug("response: " + json.dumps({"access_token": _JWT, "client_secret": _SECRET}))
        logger.debug("redirected to https://host.example/cb#access_token=%s", _JWT)
        logger.debug("env %s", {"PLTR_CLIENT_SECRET": _SECRET, "PLTR_CLIENT_ID": _CLIENT_ID})
        try:
            raise RuntimeError(f"401 for Bearer {_JWT}")
        except RuntimeError:
            logger.error("call failed", exc_info=True)
        text = stream.getvalue()
        assert not _leaked(text, _JWT)
        assert not _leaked(text, _SECRET)
        assert _CLIENT_ID in text, "a client id is not a credential and must stay readable"
