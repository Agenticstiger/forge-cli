# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid policy apply`` onto Lakekeeper, against an in-process Lakekeeper.

Covers the diff (grant what is missing, revoke what is not declared, never
touch ownership), idempotence, check mode reading only, the states an operator
meets (no credential, a table not created yet, an unknown principal, a refusal),
and the credential rules (https, the destination policy, redaction).
"""

from __future__ import annotations

from typing import Any, Dict, List

import httpx
import pytest

from fluid_build.policy import lakekeeper_apply
from fluid_build.policy.lakekeeper_apply import apply_lakekeeper_bindings, service_base

from . import _fake_lakekeeper as fake

pytestmark = pytest.mark.unit

TABLE_ID = "0190c5b4-0000-7000-8000-0000000000b1"
ROLE_ID = "0190c5b4-0000-7000-8000-0000000000c1"


@pytest.fixture
def server() -> fake.FakeLakekeeper:
    lk = fake.FakeLakekeeper()
    lk.add_table(("curated",), "events_daily", TABLE_ID)
    lk.add_user("oidc~trino", "service-account-trino")
    lk.add_user("oidc~alice", "Alice", "alice@corp.com")
    lk.add_user("oidc~probe", "service-account-lab-probe")
    lk.add_role(ROLE_ID, "analysts")
    return lk


@pytest.fixture
def operator_env(monkeypatch) -> Dict[str, str]:
    """The operator's settings: a client-credentials reference, allowed for the catalog."""
    env = {
        "LK_CRED": f"{fake.CLIENT_ID}:{fake.CLIENT_SECRET}",
        "FLUID_LAKEKEEPER_CREDENTIAL_REF": "env://LK_CRED",
        "FLUID_LAKEKEEPER_OAUTH2_SERVER_URI": fake.TOKEN_URL,
        "FLUID_LAKEKEEPER_SCOPE": "lakekeeper",
        "FLUID_ACQUISITION_SECRET_REF_ALLOWLIST": "LK_CRED@lakekeeper.test",
    }
    for name in ("FLUID_LAKEKEEPER_TOKEN_REF", "FLUID_SECRETS_FILE", "FLUID_SECRET_SCHEMES"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return env


def _binding(principal: str, *relations: str, identity: str = "", table: str = "events_daily"):
    return {
        "provider": "lakekeeper",
        "resource_type": "lakekeeper.table",
        "resource_id": f"sales-wh/curated.{table}",
        "exposeId": table,
        "catalog_uri": fake.CATALOG_URI,
        "warehouse": fake.WAREHOUSE,
        "namespace": ["curated"],
        "table": table,
        "principal": principal,
        "identities": [identity or principal],
        "relations": list(relations),
    }


def _run(server, bindings, mode="enforce", **kw) -> Dict[str, Any]:
    lines: List[str] = []
    result = apply_lakekeeper_bindings(
        bindings, mode=mode, client=server.client(), say=lines.append, **kw
    )
    result["_lines"] = lines
    return result


class TestEnforce:
    def test_grants_the_declared_relations(self, server, operator_env):
        result = _run(server, [_binding("serviceAccount:service-account-trino", "select")])
        assert result["status"] == "ok" and result["applied"] is True
        assert server.held(TABLE_ID) == {("select", "user", "oidc~trino")}
        assert result["grants"] == [
            {
                "table": "sales-wh/curated.events_daily",
                "relation": "select",
                "kind": "user",
                "id": "oidc~trino",
            }
        ]
        assert any(
            "+ sales-wh/curated.events_daily: grant select to" in line for line in result["_lines"]
        )

    def test_revokes_a_data_relation_the_contract_does_not_declare(self, server, operator_env):
        server.assignments[TABLE_ID] = {
            ("select", "user", "oidc~probe"),
            ("modify", "user", "oidc~trino"),
        }
        result = _run(server, [_binding("serviceAccount:service-account-trino", "select")])
        assert server.held(TABLE_ID) == {("select", "user", "oidc~trino")}
        assert {(r["relation"], r["id"]) for r in result["revokes"]} == {
            ("select", "oidc~probe"),
            ("modify", "oidc~trino"),
        }
        # A principal the contract does not name is shown by its Lakekeeper name.
        assert any("service-account-lab-probe" in line for line in result["_lines"])

    def test_never_touches_ownership_or_grant_management(self, server, operator_env):
        kept = {
            ("ownership", "user", "oidc~spark"),
            ("manage_grants", "role", ROLE_ID),
            ("pass_grants", "user", "oidc~x"),
            ("manage_tags", "user", "oidc~y"),
        }
        server.assignments[TABLE_ID] = set(kept)
        _run(server, [_binding("role:analysts", "select")])
        assert server.held(TABLE_ID) == kept | {("select", "role", ROLE_ID)}

    def test_one_request_per_table_and_a_second_run_sends_nothing(self, server, operator_env):
        server.assignments[TABLE_ID] = {("select", "user", "oidc~probe")}
        bindings = [
            _binding("role:analysts", "select", "modify"),
            _binding("user:alice@corp.com", "select"),
        ]
        first = _run(server, bindings)
        (body,) = server.posts_to_assignments()
        assert body == {
            "writes": [
                {"type": "modify", "role": ROLE_ID},
                {"type": "select", "role": ROLE_ID},
                {"type": "select", "user": "oidc~alice"},
            ],
            "deletes": [{"type": "select", "user": "oidc~probe"}],
        }
        assert len(first["grants"]) == 3 and len(first["revokes"]) == 1

        second = _run(server, bindings)
        assert len(server.posts_to_assignments()) == 1
        assert second["grants"] == [] and second["revokes"] == [] and second["unchanged"] == 3
        assert any("3 grant(s) already in place" in line for line in second["_lines"])

    def test_a_group_principal_is_the_lakekeeper_role_of_that_name(self, server, operator_env):
        _run(server, [_binding("group:analysts", "select")])
        assert server.held(TABLE_ID) == {("select", "role", ROLE_ID)}

    def test_a_role_and_a_user_by_id_are_checked_then_used(self, server, operator_env):
        _run(
            server,
            [
                _binding("a", "select", identity=f"role:{ROLE_ID}"),
                _binding("b", "select", identity="user:oidc~trino"),
            ],
        )
        assert server.held(TABLE_ID) == {
            ("select", "role", ROLE_ID),
            ("select", "user", "oidc~trino"),
        }

    def test_a_409_is_re_read_and_re_planned(self, server, operator_env):
        server.conflicts = 1
        result = _run(server, [_binding("role:analysts", "select")])
        assert result["status"] == "ok"
        assert server.held(TABLE_ID) == {("select", "role", ROLE_ID)}
        assert len(server.posts_to_assignments()) == 2

    def test_more_than_100_changes_go_in_chunks_grants_first(self, server, operator_env):
        for i in range(120):
            server.add_user(f"oidc~u{i:03d}", f"user-{i:03d}")
        server.assignments[TABLE_ID] = {("select", "user", f"oidc~old{i}") for i in range(30)}
        bindings = [_binding(f"user:user-{i:03d}", "select") for i in range(120)]
        result = _run(server, bindings)
        posts = server.posts_to_assignments()
        assert [len(p["writes"]) + len(p["deletes"]) for p in posts] == [100, 50]
        assert posts[0]["deletes"] == [] and len(posts[1]["writes"]) == 20
        assert len(result["grants"]) == 120 and len(result["revokes"]) == 30
        assert server.held(TABLE_ID) == {("select", "user", f"oidc~u{i:03d}") for i in range(120)}

    def test_table_ids_are_found_across_pages(self, server, operator_env):
        server.page_size = 2
        for i in range(5):
            server.add_table(("curated",), f"a{i}", f"0190c5b4-0000-7000-8000-00000000aa0{i}")
        _run(server, [_binding("role:analysts", "select")])
        assert server.held(TABLE_ID) == {("select", "role", ROLE_ID)}


class TestCheckMode:
    def test_reads_plans_and_sends_nothing(self, server, operator_env):
        server.assignments[TABLE_ID] = {("select", "user", "oidc~probe")}
        result = _run(server, [_binding("role:analysts", "select")], mode="check")
        assert server.posts_to_assignments() == []
        assert server.held(TABLE_ID) == {("select", "user", "oidc~probe")}
        assert result["status"] == "ok" and result["applied"] is False
        assert len(result["grants"]) == 1 and len(result["revokes"]) == 1
        lines = "\n".join(result["_lines"])
        assert "would grant select" in lines and "would revoke select" in lines
        assert "check mode changed nothing" in lines

    def test_every_request_is_a_read_or_the_token(self, server, operator_env):
        _run(server, [_binding("user:alice@corp.com", "select")], mode="check")
        writes = [
            (m, p)
            for m, p, _ in server.requests
            if m != "GET" and not p.endswith(("/token", "/search/user", "/search/role"))
        ]
        assert writes == []

    def test_a_table_not_created_yet_is_pending_not_an_error(self, server, operator_env):
        result = _run(server, [_binding("role:analysts", "select", table="later")], mode="check")
        assert result["status"] == "ok"
        assert result["pending"] == ["sales-wh/curated.later"]
        assert any("does not exist in Lakekeeper yet" in line for line in result["_lines"])

    def test_without_a_credential_it_says_so_and_does_not_fail(self, server, monkeypatch):
        for name in ("FLUID_LAKEKEEPER_CREDENTIAL_REF", "FLUID_LAKEKEEPER_TOKEN_REF"):
            monkeypatch.delenv(name, raising=False)
        result = _run(server, [_binding("role:analysts", "select")], mode="check")
        assert result["status"] == "noop"
        assert "FLUID_LAKEKEEPER_CREDENTIAL_REF" in result["note"]
        assert server.requests == []


class TestFailures:
    def test_without_a_credential_enforce_fails(self, server, monkeypatch):
        for name in ("FLUID_LAKEKEEPER_CREDENTIAL_REF", "FLUID_LAKEKEEPER_TOKEN_REF"):
            monkeypatch.delenv(name, raising=False)
        result = _run(server, [_binding("role:analysts", "select")])
        assert result["status"] == "error"
        assert "no grant was applied" in result["errors"][0]

    def test_a_missing_table_is_pending_in_enforce_mode_too(self, server, operator_env):
        """Its writer creates it, not fluid apply: a first deploy reaches stage 8 first."""
        result = _run(server, [_binding("role:analysts", "select", table="later")])
        assert result["status"] == "ok"
        assert result["pending"] == ["sales-wh/curated.later"]
        assert server.posts_to_assignments() == []
        lines = "\n".join(result["_lines"])
        assert "run policy-apply again then" in lines and "fluid apply creates" not in lines

    def test_an_unrendered_template_in_the_names_is_refused(self, server, operator_env):
        binding = dict(_binding("role:analysts", "select"), warehouse="{{ env.LK_WH }}")
        result = _run(server, [binding])
        assert "unrendered template" in result["errors"][0]
        assert server.posts_to_assignments() == []

    def test_an_unknown_principal_changes_nothing_on_its_table(self, server, operator_env):
        server.assignments[TABLE_ID] = {("select", "user", "oidc~nobody-knows")}
        result = _run(
            server, [_binding("role:analysts", "select"), _binding("user:ghost", "select")]
        )
        assert result["status"] == "error"
        assert server.posts_to_assignments() == []
        assert server.held(TABLE_ID) == {("select", "user", "oidc~nobody-knows")}
        assert "no Lakekeeper user is named 'ghost'" in result["errors"][0]
        assert "service-account-<client-id>" in result["errors"][0]

    def test_an_ambiguous_user_names_the_candidates(self, server, operator_env):
        server.add_user("oidc~alice2", "alice@corp.com")
        result = _run(server, [_binding("user:alice@corp.com", "select")])
        assert "oidc~alice" in result["errors"][0] and "oidc~alice2" in result["errors"][0]

    def test_a_user_id_lakekeeper_does_not_know_is_refused(self, server, operator_env):
        result = _run(server, [_binding("u", "select", identity="user:oidc~typo")])
        assert "Lakekeeper has no user 'oidc~typo'" in result["errors"][0]

    def test_a_missing_role_says_lakekeeper_reads_no_idp_group(self, server, operator_env):
        result = _run(server, [_binding("group:finance", "select")])
        assert "no role named 'finance'" in result["errors"][0]
        assert "reads no group from the identity provider" in result["errors"][0]

    def test_a_refusal_says_what_the_identity_needs(self, server, operator_env):
        server.assignments_status = 403
        result = _run(server, [_binding("role:analysts", "select")])
        assert "manage_grants" in result["errors"][0]

    def test_a_refused_client_is_named_and_its_secret_is_not(
        self, server, operator_env, monkeypatch
    ):
        monkeypatch.setenv(
            "LK_CRED", f"{fake.CLIENT_ID}:wrong-fake-value"
        )  # pragma: allowlist secret
        result = _run(server, [_binding("role:analysts", "select")])
        (error,) = result["errors"]
        assert "refused client 'fluid-apply-sales'" in error
        assert "unauthorized_client" in error and "wrong-fake-value" not in error

    def test_a_token_lakekeeper_refuses_points_at_the_scope(self, server, operator_env):
        server.refuse_tokens = True
        result = _run(server, [_binding("role:analysts", "select")])
        assert "refused the token" in result["errors"][0]
        assert "FLUID_LAKEKEEPER_SCOPE" in result["errors"][0]

    def test_a_redirect_is_not_followed(self, server, operator_env):
        server.redirect_whoami = True
        result = _run(server, [_binding("role:analysts", "select")])
        assert "redirect" in result["errors"][0]
        assert not any("elsewhere.test" in path for _, path, _ in server.requests)

    def test_an_unreachable_server_is_said_plainly(self, operator_env):
        def refuse(request):
            raise httpx.ConnectError("connection refused", request=request)

        client = httpx.Client(transport=httpx.MockTransport(refuse))
        lines: List[str] = []
        result = apply_lakekeeper_bindings(
            [_binding("role:analysts", "select")], mode="enforce", client=client, say=lines.append
        )
        assert "could not reach keycloak.test" in result["errors"][0]

    @pytest.mark.parametrize(
        "env, message",
        [
            ({"FLUID_LAKEKEEPER_TOKEN_REF": "env://T"}, "not both"),
            ({"FLUID_LAKEKEEPER_SCOPE": ""}, "needs FLUID_LAKEKEEPER_SCOPE"),
        ],
    )
    def test_incomplete_settings_are_refused(self, server, operator_env, monkeypatch, env, message):
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        result = _run(server, [_binding("role:analysts", "select")])
        assert message in result["errors"][0]
        assert server.requests == []


class TestMalformedBindings:
    """A binding that names no identity or relation would read as "nobody may read
    this table": refused, so a hand-edited file cannot revoke every grant."""

    @pytest.mark.parametrize(
        "change, message",
        [
            ({"identities": []}, "names no identities"),
            ({"relations": []}, "names no relations"),
            ({"relations": ["ownership"]}, "names relations ['ownership']"),
        ],
    )
    def test_it_is_refused_and_nothing_changes(self, server, operator_env, change, message):
        server.assignments[TABLE_ID] = {("select", "user", "oidc~probe")}
        result = _run(server, [dict(_binding("role:analysts", "select"), **change)])
        assert message in result["errors"][0]
        assert server.posts_to_assignments() == []
        assert server.held(TABLE_ID) == {("select", "user", "oidc~probe")}


class TestCredentialRules:
    @pytest.mark.parametrize(
        "uri, message",
        [
            ("http://lakekeeper.test/catalog", "must be an https:// URL"),
            ("https://user:pw@lakekeeper.test/catalog", "user:password@"),
            ("https://{{ env.LK }}/catalog", "unrendered template"),
        ],
    )
    def test_the_catalog_uri_must_be_plain_https(self, server, operator_env, uri, message):
        binding = dict(_binding("role:analysts", "select"), catalog_uri=uri)
        result = _run(server, [binding])
        assert message in result["errors"][0]
        assert server.requests == []
        # The uri is the contract's: a message names its host, never userinfo.
        assert "user:pw" not in "\n".join(result["_lines"]) + repr(result)

    def test_an_http_token_endpoint_is_refused_before_the_secret_is_sent(
        self, server, operator_env, monkeypatch
    ):
        monkeypatch.setenv("FLUID_LAKEKEEPER_OAUTH2_SERVER_URI", "http://keycloak.test/token")
        result = _run(server, [_binding("role:analysts", "select")])
        assert "FLUID_LAKEKEEPER_OAUTH2_SERVER_URI must be an https:// URL" in result["errors"][0]
        assert server.requests == []

    def test_a_catalog_host_the_operator_did_not_allow_gets_no_token(
        self, server, operator_env, monkeypatch
    ):
        monkeypatch.setenv("FLUID_ACQUISITION_SECRET_REF_ALLOWLIST", "LK_CRED@other.test")
        result = _run(server, [_binding("role:analysts", "select")])
        assert "lakekeeper.test" in result["errors"][0]
        assert server.requests == []

    def test_a_bearer_token_reference_signs_in_without_the_token_endpoint(
        self, server, operator_env, monkeypatch
    ):
        monkeypatch.delenv("FLUID_LAKEKEEPER_CREDENTIAL_REF")
        monkeypatch.setenv("LK_TOKEN", fake.TOKEN)
        monkeypatch.setenv("FLUID_LAKEKEEPER_TOKEN_REF", "env://LK_TOKEN")
        monkeypatch.setenv("FLUID_ACQUISITION_SECRET_REF_ALLOWLIST", "LK_TOKEN@lakekeeper.test")
        result = _run(server, [_binding("role:analysts", "select")])
        assert result["status"] == "ok"
        assert not any(path.startswith("https://keycloak.test") for _, path, _ in server.requests)

    def test_the_secret_and_the_token_are_registered_with_the_redactor(
        self, server, operator_env, monkeypatch
    ):
        registered: List[str] = []
        monkeypatch.setattr(lakekeeper_apply, "_register", registered.append)
        _run(server, [_binding("role:analysts", "select")])
        assert fake.CLIENT_SECRET in registered and fake.TOKEN in registered

    def test_no_line_shown_holds_the_secret_or_the_token(self, server, operator_env):
        result = _run(server, [_binding("role:analysts", "select")])
        shown = "\n".join(result["_lines"]) + repr(result)
        assert fake.CLIENT_SECRET not in shown and fake.TOKEN not in shown


@pytest.mark.parametrize(
    "uri, base",
    [
        ("https://lk.test/catalog", "https://lk.test"),
        ("https://lk.test/catalog/", "https://lk.test"),
        ("https://lk.test:8443/lakekeeper/catalog", "https://lk.test:8443/lakekeeper"),
        ("https://lk.test", "https://lk.test"),
    ],
)
def test_the_service_base_is_the_catalog_uri_without_catalog(uri, base):
    assert service_base(uri) == base


def _json(status: int, doc: Any) -> httpx.Response:
    return httpx.Response(status, json=doc)


class TestOddAnswers:
    """A body of the wrong shape (a gateway, a captive portal, another server) is said
    as an error on its table, never an exception that stops the run."""

    @pytest.mark.parametrize(
        "path, doc",
        [
            ("/management/v1/warehouse", [1]),
            ("/management/v1/warehouse", {"warehouses": 5}),
            ("/catalog/v1/config", [1]),
            ("/catalog/v1/config", {"overrides": "x", "defaults": "y"}),
            ("/tables", [1]),
            ("/tables", {"identifiers": 5}),
            ("/assignments", [1]),
            ("/assignments", {"assignments": 5}),
            ("/management/v1/search/role", [1]),
            ("/management/v1/search/role", {"roles": "x"}),
        ],
    )
    def test_a_wrong_shape_is_an_error_not_a_crash(self, server, operator_env, path, doc):
        def odd(method, request_path, body):
            if (
                request_path.endswith(path)
                and method == "GET"
                or (request_path == path and method == "POST")
            ):
                return _json(200, doc)
            return None

        server.hook = odd
        result = _run(server, [_binding("role:analysts", "select")])
        assert result["status"] == "error"
        assert any("Lakekeeper" in e for e in result["errors"])
        assert server.posts_to_assignments() == []

    def test_a_404_without_lakekeeper_body_is_not_a_missing_table(self, server, operator_env):
        server.hook = lambda m, p, b: (
            httpx.Response(404, text="no route") if p.endswith("/tables") else None
        )
        result = _run(server, [_binding("role:analysts", "select")], mode="check")
        assert result["pending"] == []
        assert "did not come from Lakekeeper" in result["errors"][0]

    def test_a_repeated_page_token_ends_the_listing(self, server, operator_env):
        def loop(method, path, body):
            if path.endswith("/tables"):
                return _json(200, {"identifiers": [], "table-uuids": [], "next-page-token": "same"})
            return None

        server.hook = loop
        result = _run(server, [_binding("role:analysts", "select")])
        assert "repeated a page token" in result["errors"][0]
        assert sum(1 for _, p, _ in server.requests if p.endswith("/tables")) == 2

    def test_a_port_that_is_not_a_number_is_refused_before_any_request(self, server, operator_env):
        binding = dict(
            _binding("role:analysts", "select"), catalog_uri="https://lakekeeper.test:abc/catalog"
        )
        result = _run(server, [binding])
        assert "not a URL policy-apply can read" in result["errors"][0]
        assert server.requests == []

    def test_a_name_lookup_that_fails_after_a_revoke_keeps_the_report(self, server, operator_env):
        server.assignments[TABLE_ID] = {("select", "user", "oidc~probe")}
        server.hook = lambda m, p, b: (
            _json(200, [1]) if p == "/management/v1/user/oidc~probe" else None
        )
        result = _run(server, [_binding("role:analysts", "select")])
        assert result["status"] == "ok"
        assert [r["id"] for r in result["revokes"]] == ["oidc~probe"]


class TestOncePerRun:
    def test_a_namespace_is_listed_once_for_all_its_tables(self, server, operator_env):
        other = server.add_table(
            ("curated",), "events_hourly", "0190c5b4-0000-7000-8000-0000000000b2"
        )
        _run(
            server,
            [
                _binding("role:analysts", "select"),
                _binding("role:analysts", "select", table="events_hourly"),
            ],
        )
        assert sum(1 for _, p, _ in server.requests if p.endswith("/tables")) == 1
        assert server.held(other) == {("select", "role", ROLE_ID)}

    def test_a_warehouse_that_fails_is_asked_once(self, server, operator_env):
        server.add_table(("curated",), "events_hourly", "0190c5b4-0000-7000-8000-0000000000b2")
        server.hook = lambda m, p, b: (
            _json(503, {"error": {"type": "Unavailable", "message": "x"}})
            if p == "/management/v1/warehouse"
            else None
        )
        result = _run(
            server,
            [
                _binding("role:analysts", "select"),
                _binding("role:analysts", "select", table="events_hourly"),
            ],
        )
        assert len(result["errors"]) == 2
        assert sum(1 for _, p, _ in server.requests if p.endswith("/management/v1/warehouse")) == 1

    def test_one_table_spelled_two_ways_is_one_table(self, server, operator_env):
        a = _binding("user:oidc~trino", "select")
        b = dict(
            _binding("user:oidc~trino", "modify"),
            catalog_uri="https://LAKEKEEPER.test:443/catalog/",
        )
        result = _run(server, [a, b])
        assert result["tables"] == 1
        assert server.held(TABLE_ID) == {
            ("select", "user", "oidc~trino"),
            ("modify", "user", "oidc~trino"),
        }
        assert result["revokes"] == []


class TestCanonicalIds:
    def test_a_role_id_in_another_case_is_lakekeepers_spelling(self, server, operator_env):
        bindings = [_binding("r", "select", identity=f"role:{ROLE_ID.upper()}")]
        _run(server, bindings)
        assert server.held(TABLE_ID) == {("select", "role", ROLE_ID)}
        second = _run(server, bindings)
        assert second["grants"] == [] and second["revokes"] == []
        assert len(server.posts_to_assignments()) == 1

    def test_a_user_id_that_cannot_be_checked_changes_nothing(self, server, operator_env):
        server.assignments[TABLE_ID] = {("select", "user", "oidc~trino")}
        server.hook = lambda m, p, b: (
            _json(403, {"error": {"type": "Forbidden", "message": "no"}})
            if p.startswith("/management/v1/user/")
            else None
        )
        result = _run(server, [_binding("t", "select", identity="user:oidc~trinno")])
        assert "permission to read users" in result["errors"][0]
        assert server.posts_to_assignments() == []
        assert server.held(TABLE_ID) == {("select", "user", "oidc~trino")}


class TestPartialApply:
    def test_changes_that_landed_before_a_failure_are_reported(self, server, operator_env):
        for i in range(120):
            server.add_user(f"oidc~u{i:03d}", f"user-{i:03d}")
        posts = []

        def second_fails(method, path, body):
            if method == "POST" and path.endswith("/assignments"):
                posts.append(body)
                if len(posts) == 2:
                    return _json(500, {"error": {"type": "Internal", "message": "boom"}})
            return None

        server.hook = second_fails
        bindings = [_binding(f"user:user-{i:03d}", "select") for i in range(120)]
        result = _run(server, bindings)
        assert result["status"] == "error"
        assert len(result["grants"]) == 100
        assert "100 change(s) were applied before that" in result["errors"][0]
        assert len(server.held(TABLE_ID)) == 100


class TestRefusedHost:
    def test_the_message_says_the_operator_named_the_credential(
        self, server, operator_env, monkeypatch
    ):
        monkeypatch.setenv("FLUID_ACQUISITION_SECRET_REF_ALLOWLIST", "LK_CRED@other.test")
        result = _run(server, [_binding("role:analysts", "select")])
        error = result["errors"][0]
        assert "the operator has not allowed to go to lakekeeper.test" in error
        assert "FLUID_ACQUISITION_SECRET_REF_ALLOWLIST" in error
        assert "the contract would send" not in error


@pytest.mark.parametrize(
    "a, b, same",
    [
        ("https://[::1]:8443/catalog", "https://[::1:8443]/catalog", False),
        ("https://LK.test/catalog/", "https://lk.test:443/catalog", True),
        ("https://lk.test:8443/catalog", "https://lk.test/catalog", False),
    ],
)
def test_catalog_key_names_one_server_one_way(a, b, same):
    from fluid_build.policy.lakekeeper_apply import catalog_key

    assert (catalog_key(a) == catalog_key(b)) is same


def test_a_namespace_that_is_not_a_list_is_refused_not_a_crash(server, operator_env):
    result = _run(server, [dict(_binding("role:analysts", "select"), namespace=5)])
    assert result["status"] == "error"
    assert server.posts_to_assignments() == []
