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

"""The masking strategies, rules and refusals behind ``build_runners/_masking.py``.

What a strategy makes of a value is pinned against an independent computation
(``hashlib``, DuckDB's own ``sha256``, the ``tokenize_pii`` hook, a decrypt),
and every refusal is pinned to name the column, the strategy or the variable
and never the value of a salt or key.
"""

from __future__ import annotations

import base64
import hashlib
import re
from typing import Any, Dict, List

import pytest

from fluid_build.build_runners import _masking as m

pytestmark = pytest.mark.unit

SALT = "salt-for-tests-0123456789"
TOKEN_KEY = "tokenization-key-for-tests-0123456789abcdef"
AES_KEY = bytes(range(32))
AES_KEY_B64 = base64.b64encode(AES_KEY).decode("ascii")
MSISDN = "+46701234567"


def _expose(rules: List[Dict[str, Any]], schema: Any = None) -> Dict[str, Any]:
    expose: Dict[str, Any] = {"exposeId": "subs", "policy": {"privacy": {"masking": rules}}}
    if schema is not None:
        expose["contract"] = {"schema": schema}
    return expose


def _env(**extra: str) -> Dict[str, str]:
    env = {
        m.DEFAULT_SALT_ENV: SALT,
        m.DEFAULT_TOKENIZATION_KEY_ENV: TOKEN_KEY,
        m.DEFAULT_ENCRYPTION_KEY_ENV: AES_KEY_B64,
    }
    env.update(extra)
    return env


def _masker(rules: List[Dict[str, Any]], env: Dict[str, str] | None = None) -> m.LandingMasker:
    masker = m.LandingMasker.for_expose(_expose(rules), environ=_env() if env is None else env)
    assert masker is not None
    return masker


def _apply(masker: m.LandingMasker, column: str, value: str) -> str:
    return masker._functions[column](value)


# ── What each strategy makes of a value ────────────────────────────────


def test_hash_is_sha256_of_salt_then_value():
    masker = _masker([{"column": "msisdn", "strategy": "hash"}])
    expected = hashlib.sha256((SALT + MSISDN).encode("utf-8")).hexdigest()
    assert _apply(masker, "msisdn", MSISDN) == expected


def test_hash_matches_duckdb_sha256_so_a_consumer_can_reproduce_it_in_sql():
    duckdb = pytest.importorskip("duckdb")
    masker = _masker([{"column": "msisdn", "strategy": "hash"}])
    in_sql = duckdb.connect().execute("SELECT sha256(? || ?)", [SALT, MSISDN]).fetchone()[0]
    assert _apply(masker, "msisdn", MSISDN) == in_sql


def test_hash_is_deterministic_so_joins_still_work():
    masker = _masker([{"column": "msisdn", "strategy": "hash"}])
    assert _apply(masker, "msisdn", MSISDN) == _apply(masker, "msisdn", MSISDN)
    assert _apply(masker, "msisdn", MSISDN) != _apply(masker, "msisdn", "+46701234568")


def test_hash_salt_can_come_from_a_variable_the_rule_names():
    rule = {"column": "msisdn", "strategy": "hash", "params": {"saltEnv": "OTHER_SALT"}}
    other = "another-salt-0123456789"
    masker = _masker([rule], env={"OTHER_SALT": other})
    assert _apply(masker, "msisdn", MSISDN) == hashlib.sha256((other + MSISDN).encode()).hexdigest()


@pytest.mark.parametrize(
    "params, value, expected",
    [
        ({}, MSISDN, "********4567"),
        ({"keepLast": 2, "keepFirst": 3}, MSISDN, "+46*******67"),
        ({"keepLast": 0}, "secret", "******"),
        ({}, "1234", "****"),  # no longer than keepLast: masked whole
        ({}, "12", "**"),
        ({}, "", ""),
    ],
)
def test_mask_keeps_the_ends_asked_for_and_preserves_length(params, value, expected):
    masker = _masker([{"column": "c", "strategy": "mask", "params": params}])
    assert _apply(masker, "c", value) == expected


def test_tokenize_is_the_token_the_tokenize_pii_hook_makes():
    from fluid_build.build_runners.hooks.tokenize_pii import (
        _TOKEN_HEX_LEN,
        _TOKEN_KEY_ENV,
        _hmac_token,
    )

    assert m.DEFAULT_TOKENIZATION_KEY_ENV == _TOKEN_KEY_ENV
    assert m.TOKEN_HEX_LEN == _TOKEN_HEX_LEN
    masker = _masker([{"column": "msisdn", "strategy": "tokenize"}])
    assert _apply(masker, "msisdn", MSISDN) == _hmac_token(MSISDN, key=TOKEN_KEY.encode())


def test_encrypt_round_trips_with_the_key_and_the_column():
    masker = _masker([{"column": "msisdn", "strategy": "encrypt"}])
    token = _apply(masker, "msisdn", MSISDN)
    assert token.startswith(m.ENCRYPTED_PREFIX)
    assert MSISDN not in token
    assert m.decrypt_value(token, key=AES_KEY, column="msisdn") == MSISDN


def test_encrypt_uses_a_fresh_nonce_per_value():
    masker = _masker([{"column": "msisdn", "strategy": "encrypt"}])
    assert _apply(masker, "msisdn", MSISDN) != _apply(masker, "msisdn", MSISDN)


def test_encrypt_binds_the_ciphertext_to_its_column():
    from cryptography.exceptions import InvalidTag

    token = m.encrypt_value(MSISDN, key=AES_KEY, column="msisdn")
    with pytest.raises(InvalidTag):
        m.decrypt_value(token, key=AES_KEY, column="customer_id")
    with pytest.raises(InvalidTag):
        m.decrypt_value(token, key=bytes(32), column="msisdn")


@pytest.mark.parametrize(
    "key", [AES_KEY_B64, base64.urlsafe_b64encode(AES_KEY).decode().rstrip("=")]
)
def test_encrypt_key_accepts_standard_and_urlsafe_base64(key):
    masker = _masker(
        [{"column": "c", "strategy": "encrypt"}], env={m.DEFAULT_ENCRYPTION_KEY_ENV: key}
    )
    assert m.decrypt_value(_apply(masker, "c", "x"), key=AES_KEY, column="c") == "x"


# ── Shapes: what fluid verify accepts ──────────────────────────────────


@pytest.mark.parametrize(
    "rule",
    [
        {"column": "c", "strategy": "hash"},
        {"column": "c", "strategy": "tokenize"},
        {"column": "c", "strategy": "encrypt"},
        {"column": "c", "strategy": "mask"},
        {"column": "c", "strategy": "mask", "params": {"keepFirst": 3, "keepLast": 2}},
        {"column": "c", "strategy": "mask", "params": {"keepLast": 0}},
    ],
)
@pytest.mark.parametrize("value", [MSISDN, "a", "", "multi\nline value", "é∂ unicode"])
def test_every_treated_value_has_its_strategys_shape(rule, value):
    masker = _masker([rule])
    (parsed,) = masker.rules
    assert parsed.matches_shape(_apply(masker, "c", value))


@pytest.mark.parametrize("strategy", ["hash", "tokenize", "encrypt", "mask"])
def test_a_cleartext_msisdn_does_not_have_any_strategys_shape(strategy):
    (rule,) = m.masking_rules(_expose([{"column": "c", "strategy": strategy}]))
    assert not rule.matches_shape(MSISDN)


def test_a_shape_is_a_whole_value_match_not_a_search():
    (rule,) = m.masking_rules(_expose([{"column": "c", "strategy": "hash"}]))
    digest = "a" * 64
    assert rule.matches_shape(digest)
    for smuggled in (f"{MSISDN}\n{digest}", f"{digest}\n", f"{digest}{MSISDN}"):
        assert not rule.matches_shape(smuggled)


def test_shapes_are_distinct_so_verify_can_tell_the_strategies_apart():
    rules = m.masking_rules(
        _expose([{"column": "a", "strategy": "hash"}, {"column": "b", "strategy": "tokenize"}])
    )
    hashed, tokenized = rules
    assert not hashed.matches_shape("a" * 32)
    assert not tokenized.matches_shape("a" * 64)


def test_the_shapes_compile_in_duckdb_too():
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    for strategy in m.LANDING_STRATEGIES:
        (rule,) = m.masking_rules(_expose([{"column": "c", "strategy": strategy}]))
        treated = _apply(_masker([{"column": "c", "strategy": strategy}]), "c", MSISDN)
        ok, cleartext = con.execute(
            "SELECT regexp_full_match(?, ?), regexp_full_match(?, ?)",
            [treated, rule.shape, MSISDN, rule.shape],
        ).fetchone()
        assert (ok, cleartext) == (True, False), strategy


# ── Rules the build refuses ─────────────────────────────────────────────


def test_k_anonymity_is_refused_with_a_typed_error_naming_it():
    with pytest.raises(m.MaskingStrategyUnsupported) as info:
        m.masking_rules(_expose([{"column": "zip", "strategy": "k_anonymity"}]))
    assert info.value.strategy == "k_anonymity"
    assert info.value.column == "zip"
    assert "k_anonymity" in str(info.value)
    assert info.value.code == "masking_strategy_unsupported"


def test_k_anonymity_is_returned_for_verify_but_is_not_appliable():
    (rule,) = m.masking_rules(
        _expose([{"column": "zip", "strategy": "k_anonymity"}]), allow_unsupported=True
    )
    assert not rule.appliable
    assert rule.shape is None
    assert not rule.matches_shape("anything")


@pytest.mark.parametrize(
    "rule, needle",
    [
        ({"column": "c", "strategy": "scramble"}, "scramble"),
        ({"column": "c; DROP TABLE t", "strategy": "hash"}, "not a plain identifier"),
        ({"strategy": "hash"}, "needs a column"),
        ({"column": "c", "strategy": "hash", "params": {"salt": "x" * 20}}, "literal 'salt'"),
        ({"column": "c", "strategy": "tokenize", "params": {"key": "x" * 40}}, "literal 'key'"),
        ({"column": "c", "strategy": "mask", "params": {"keeplast": 2}}, "unknown params keeplast"),
        ({"column": "c", "strategy": "hash", "params": {"keepLast": 2}}, "unknown params keepLast"),
        ({"column": "c", "strategy": "mask", "params": {"keepLast": -1}}, "keepLast"),
        ({"column": "c", "strategy": "mask", "params": {"keepLast": 65}}, "keepLast"),
        ({"column": "c", "strategy": "mask", "params": {"keepLast": True}}, "keepLast"),
        ({"column": "c", "strategy": "mask", "params": {"keepLast": "4"}}, "keepLast"),
        ({"column": "c", "strategy": "hash", "params": {"saltEnv": "not a name"}}, "saltEnv"),
        ({"column": "c", "strategy": "hash", "params": {"saltEnv": "lower_case"}}, "upper case"),
        ({"column": "c", "strategy": "hash", "params": "salty"}, "must be a mapping"),
    ],
)
def test_a_rule_that_cannot_be_applied_as_written_is_refused(rule, needle):
    with pytest.raises(m.MaskingPolicyError) as info:
        m.masking_rules(_expose([rule]))
    assert needle in str(info.value)


def test_a_literal_secret_in_params_is_not_echoed_in_the_refusal():
    literal = "a-literal-salt-in-the-contract"
    with pytest.raises(m.MaskingPolicyError) as info:
        m.masking_rules(_expose([{"column": "c", "strategy": "hash", "params": {"salt": literal}}]))
    assert literal not in str(info.value)


def test_a_salt_pasted_where_its_variable_name_goes_is_not_echoed():
    # The shape of ``openssl rand -hex 32``, made here rather than written out.
    pasted = hashlib.sha256(b"not a real salt").hexdigest()
    with pytest.raises(m.MaskingPolicyError) as info:
        m.masking_rules(
            _expose([{"column": "c", "strategy": "hash", "params": {"saltEnv": pasted}}])
        )
    assert pasted not in str(info.value)


def test_two_rules_on_one_column_are_refused():
    with pytest.raises(m.MaskingPolicyError, match="two masking rules"):
        m.masking_rules(
            _expose([{"column": "c", "strategy": "hash"}, {"column": "C", "strategy": "mask"}])
        )


def test_no_rules_means_no_masker():
    assert m.LandingMasker.for_expose({"exposeId": "x"}) is None
    assert m.LandingMasker.for_expose(_expose([])) is None
    assert m.LandingMasker.for_expose(None) is None


# ── Secrets ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "strategy, variable",
    [
        ("hash", m.DEFAULT_SALT_ENV),
        ("tokenize", m.DEFAULT_TOKENIZATION_KEY_ENV),
        ("encrypt", m.DEFAULT_ENCRYPTION_KEY_ENV),
    ],
)
@pytest.mark.parametrize("value", [None, ""])
def test_an_unset_or_empty_secret_refuses_the_build_naming_the_variable(strategy, variable, value):
    env = _env()
    if value is None:
        env.pop(variable)
    else:
        env[variable] = value
    with pytest.raises(m.MaskingSecretMissing) as info:
        _masker([{"column": "msisdn", "strategy": strategy}], env=env)
    assert variable in str(info.value)
    assert info.value.strategy == strategy


@pytest.mark.parametrize(
    "strategy, variable, weak",
    [
        ("hash", m.DEFAULT_SALT_ENV, "short-salt"),
        ("tokenize", m.DEFAULT_TOKENIZATION_KEY_ENV, "a-key-of-thirty-one-bytes-long!"),
        ("encrypt", m.DEFAULT_ENCRYPTION_KEY_ENV, "not base64 at all!"),
        ("encrypt", m.DEFAULT_ENCRYPTION_KEY_ENV, base64.b64encode(b"x" * 20).decode()),
    ],
)
def test_a_weak_or_malformed_secret_is_refused_without_echoing_it(strategy, variable, weak):
    with pytest.raises(m.MaskingSecretMissing) as info:
        _masker([{"column": "msisdn", "strategy": strategy}], env=_env(**{variable: weak}))
    assert weak not in str(info.value)
    assert variable in str(info.value)


def test_secrets_are_registered_for_exact_value_log_redaction():
    from fluid_build.observability.secret_redactor import (
        forget_known_secrets,
        redact_secret_text,
    )

    forget_known_secrets()
    try:
        _masker([{"column": "a", "strategy": "hash"}, {"column": "b", "strategy": "tokenize"}])
        line = f"a log line carrying {SALT} and {TOKEN_KEY}"
        redacted = redact_secret_text(line)
        assert SALT not in redacted
        assert TOKEN_KEY not in redacted
    finally:
        forget_known_secrets()


def test_neither_the_masker_repr_nor_its_facet_carries_a_secret():
    masker = _masker(
        [
            {"column": "a", "strategy": "hash"},
            {"column": "b", "strategy": "tokenize"},
            {"column": "c", "strategy": "encrypt"},
        ]
    )
    for text in (repr(masker), repr(masker.facet())):
        for secret in (SALT, TOKEN_KEY, AES_KEY_B64):
            assert secret not in text
    assert masker.facet()["columns"]["a"] == {
        "strategy": "hash",
        "saltEnv": m.DEFAULT_SALT_ENV,
    }


# ── Declared types and the fingerprint ──────────────────────────────────


@pytest.mark.parametrize(
    "declared", ["VARCHAR", "string", "String", "text", "varchar(64)", "character varying"]
)
def test_a_string_declared_type_is_accepted(declared):
    expose = _expose([{"column": "c", "strategy": "hash"}], [{"name": "c", "type": declared}])
    assert m.LandingMasker.for_expose(expose, environ=_env()) is not None


@pytest.mark.parametrize("declared", ["INTEGER", "bigint", "DATE", "timestamp", "decimal(10,2)"])
def test_a_non_string_declared_type_is_refused_saying_what_to_declare(declared):
    expose = _expose(
        [{"column": "customer_id", "strategy": "hash"}],
        [{"name": "customer_id", "type": declared}],
    )
    with pytest.raises(m.MaskingTypeIncompatible) as info:
        m.LandingMasker.for_expose(expose, environ=_env())
    message = str(info.value)
    assert "customer_id" in message and declared in message
    assert "string" in message and "VARCHAR" in message


def test_the_declared_type_is_checked_before_any_secret_is_read():
    expose = _expose([{"column": "c", "strategy": "hash"}], [{"name": "c", "type": "INTEGER"}])
    with pytest.raises(m.MaskingTypeIncompatible):
        m.LandingMasker.for_expose(expose, environ={})


def test_the_incremental_cursor_cannot_be_masked():
    with pytest.raises(m.MaskingPolicyError, match="cursor_field"):
        m.LandingMasker.for_expose(
            _expose([{"column": "updated_at", "strategy": "hash"}]),
            cursor_field="UPDATED_AT",
            environ=_env(),
        )


def test_masked_column_types_say_varchar_for_the_fingerprint():
    expose = _expose(
        [
            {"column": "MSISDN", "strategy": "hash"},
            {"column": "zip", "strategy": "mask"},
        ]
    )
    assert m.masked_column_types(expose) == {"msisdn": "VARCHAR", "zip": "VARCHAR"}
    assert m.masked_column_types(_expose([{"column": "z", "strategy": "k_anonymity"}])) == {}


# ── The SQL the runner embeds ───────────────────────────────────────────


def test_projection_replaces_only_the_masked_columns_and_never_carries_a_secret():
    masker = _masker([{"column": "msisdn", "strategy": "hash"}])
    sql = masker.projection("SELECT * FROM src", {"msisdn": "MSISDN"})
    assert sql == (
        'SELECT * REPLACE (__fluid_mask_0(CAST("MSISDN" AS VARCHAR)) AS "MSISDN") '
        "FROM (SELECT * FROM src)"
    )
    assert SALT not in sql


def test_landed_columns_refuses_a_rule_on_a_column_the_stream_lacks():
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    masker = _masker([{"column": "msisdn", "strategy": "hash"}])
    with pytest.raises(m.MaskingColumnMissing) as info:
        masker.landed_columns(con, "SELECT 1 AS id, 'x' AS name", "public.subs")
    assert "msisdn" in str(info.value) and "public.subs" in str(info.value)
    assert "id, name" in str(info.value)


def test_nulls_pass_through_untreated_and_encrypt_is_never_folded():
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    masker = _masker([{"column": "v", "strategy": "encrypt"}])
    masker.install(con)
    rows = con.execute(
        masker.projection("SELECT * FROM (VALUES ('x'), ('x'), (NULL)) t(v)", {"v": "v"})
    ).fetchall()
    first, second, null = (r[0] for r in rows)
    assert null is None
    assert first != second  # one nonce per value, even for equal inputs
    assert re.fullmatch(masker.rules[0].shape or "", first)
