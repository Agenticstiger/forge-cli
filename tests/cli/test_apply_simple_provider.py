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

"""Simple-mode ``fluid apply`` does not take a platform for a provider (#752).

``binding.platform`` says where an expose lands and a build's
``execution.runtime.platform`` where it runs. Simple mode read them as the
provider name, so a Kafka binding or a Flink build's ``kubernetes`` ended as
``provider_unknown`` (deferred by #731), and an expose on a platform no
provider serves was handed to the local provider, which wrote a placeholder
CSV for it, whenever a later expose was local. These pin the rule
``_simple_apply_provider`` follows instead.
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from fluid_build.cli import build_parser
from fluid_build.cli._common import CLIError
from fluid_build.cli.apply import _simple_apply_provider
from fluid_build.cli.apply import run as apply_run

LOG = logging.getLogger("test.apply_simple_provider")

FLINK_SQL = "INSERT INTO events_out SELECT * FROM events_in"


_FORMATS = {"kafka": "kafka_topic", "databricks": "delta_table", "postgres": "postgres_table"}


def _expose(expose_id: str, platform: str) -> Dict[str, Any]:
    location: Dict[str, Any] = {"path": f"out/{expose_id}.csv"}
    if platform == "kafka":
        location = {"topic": expose_id}
    elif platform == "databricks":
        location = {"catalog": "main", "schema": "s", "table": expose_id}
    return {
        "exposeId": expose_id,
        "kind": "topic" if platform == "kafka" else "table",
        "binding": {
            "platform": platform,
            "format": _FORMATS.get(platform, "csv"),
            "location": location,
        },
        "contract": {"schema": [{"name": "id", "type": "string"}]},
    }


def _flink(outputs: Optional[List[str]], *, shape: str = "engine") -> Dict[str, Any]:
    """A Flink job on Kubernetes: ``engine: flink`` (0.7.6), or the 0.7.5 spelling
    ``properties.language: flink_sql``."""
    build: Dict[str, Any] = {
        "id": "reconcile",
        "engine": "flink" if shape == "engine" else "sql",
        "pattern": "embedded-logic",
        "properties": {"language": "flink_sql", "sql": FLINK_SQL},
        "execution": {"runtime": {"platform": "kubernetes"}},
    }
    if outputs is not None:
        build["outputs"] = outputs
    return build


def _sql(build_id: str, outputs: List[str], platform: Optional[str] = None) -> Dict[str, Any]:
    build: Dict[str, Any] = {
        "id": build_id,
        "engine": "sql",
        "pattern": "embedded-logic",
        "properties": {"sql": "SELECT 1 AS id"},
        "outputs": outputs,
    }
    if platform:
        build["execution"] = {"runtime": {"platform": platform}}
    return build


def _contract(exposes: List[Dict[str, Any]], builds: Any = ()) -> Dict[str, Any]:
    contract: Dict[str, Any] = {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "demo.platforms",
        "name": "Platforms demo",
        "metadata": {"owner": {"team": "data-engineering"}},
        "exposes": exposes,
    }
    if builds:
        contract["builds"] = list(builds)
    return contract


def _refusal(contract: Dict[str, Any]) -> CLIError:
    with pytest.raises(CLIError) as exc:
        _simple_apply_provider(contract, LOG)
    assert exc.value.event == "apply_platform_unsupported"
    assert exc.value.exit_code == 2
    return exc.value


class TestOutsideForgeCli:
    """A job that runs outside forge-cli, and what it writes, choose no provider."""

    @pytest.mark.parametrize("shape", ["engine", "language"])
    def test_a_flink_job_writing_a_kafka_topic(self, shape: str) -> None:
        # The #731 contract: provider_unknown "kafka" before.
        contract = _contract([_expose("events_out", "kafka")], [_flink(None, shape=shape)])
        assert _simple_apply_provider(contract, LOG) == "local"

    def test_a_flink_builds_kubernetes_runtime_beside_a_local_product(self) -> None:
        # provider_unknown "kubernetes" before: the local expose gave "local",
        # so the first build's runtime.platform was read as the provider.
        contract = _contract(
            [_expose("orders", "local"), _expose("events_out", "kafka")],
            [_flink(["events_out"]), _sql("make_orders", ["orders"])],
        )
        assert _simple_apply_provider(contract, LOG) == "local"

    @pytest.mark.parametrize("order", ["local-first", "kafka-first"])
    def test_the_order_of_the_exposes_does_not_matter(self, order: str) -> None:
        exposes = [_expose("orders", "local"), _expose("events_out", "kafka")]
        if order == "kafka-first":
            exposes.reverse()
        contract = _contract(exposes, [_flink(["events_out"]), _sql("make_orders", ["orders"])])
        assert _simple_apply_provider(contract, LOG) == "local"

    def test_an_expose_a_flink_job_may_write_is_left_as_the_planner_leaves_it(self) -> None:
        # Neither build declares outputs, so the Flink job may write the topic:
        # the local planner lands nothing for it, and its platform chooses nothing.
        other = _sql("other", [])
        del other["outputs"]
        contract = _contract([_expose("events_out", "kafka")], [_flink(None), other])
        assert _simple_apply_provider(contract, LOG) == "local"


class TestNoProviderServesIt:
    """A platform no provider serves stops the run; it is never handed to another."""

    @pytest.mark.parametrize("platform", ["kafka", "databricks", "postgres", "pgvector", "other"])
    def test_an_expose(self, platform: str) -> None:
        error = _refusal(_contract([_expose("t", platform)]))
        assert f"exposes[0].binding.platform: {platform}" in error.context["error"]
        assert "local" in error.context["providers"].split(", ")

    @pytest.mark.parametrize("platform", ["kubernetes", "composer", "databricks"])
    def test_the_runtime_of_a_build_forge_cli_runs(self, platform: str) -> None:
        contract = _contract([_expose("orders", "local")], [_sql("b", ["orders"], platform)])
        error = _refusal(contract)
        assert f"builds[0].execution.runtime.platform: {platform}" in error.context["error"]

    def test_an_earlier_expose_too(self) -> None:
        # The last expose chose the provider: [databricks, local] went to the
        # local provider, which landed the Databricks table as a placeholder CSV.
        contract = _contract([_expose("t", "databricks"), _expose("orders", "local")])
        error = _refusal(contract)
        assert "exposes[0].binding.platform: databricks" in error.context["error"]

    def test_every_one_is_named(self) -> None:
        contract = _contract(
            [_expose("a", "kafka"), _expose("b", "local"), _expose("c", "databricks")],
            [_sql("x", ["b"], "composer")],
        )
        said = _refusal(contract).context["error"]
        for where in (
            "exposes[0].binding.platform: kafka",
            "exposes[2].binding.platform: databricks",
            "builds[0].execution.runtime.platform: composer",
        ):
            assert where in said


class TestUnderAPluginPolicy:
    """An allow or block list removes providers; it does not turn a platform no
    provider is named after into one."""

    @pytest.mark.parametrize(
        "variable, value",
        [("FLUID_PLUGINS_ALLOWLIST", "local"), ("FLUID_PLUGINS_BLOCKLIST", "databricks")],
    )
    def test_a_platform_no_provider_serves_is_still_refused(
        self, monkeypatch: pytest.MonkeyPatch, variable: str, value: str
    ) -> None:
        # Asking is_allowed() of the platform made any policy switch the check
        # off: [databricks, local] then landed the table as a placeholder CSV.
        monkeypatch.setenv(variable, value)
        contract = _contract([_expose("t", "databricks"), _expose("orders", "local")])
        assert "exposes[0].binding.platform: databricks" in _refusal(contract).context["error"]


class TestTheChoiceIsUnchanged:
    """Among platforms a provider serves, the provider is the one it always was."""

    @pytest.fixture(autouse=True)
    def _clouds_registered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Registered or not on this install (an extra may be missing), the
        # choice only needs the names in the registry.
        from fluid_build import providers as registry

        registry.discover_providers(LOG)
        for name in ("aws", "gcp", "snowflake"):
            monkeypatch.setitem(registry.PROVIDERS, name, registry.PROVIDERS.get(name, object))

    @pytest.mark.parametrize(
        "exposes, runtimes, expected",
        [
            ([], [], "local"),
            (["local"], [], "local"),
            (["gcp"], [], "gcp"),
            (["gcp", "snowflake"], [], "snowflake"),  # the last expose
            (["local"], ["gcp"], "gcp"),  # local exposes: the first build's
            ([], ["snowflake", "gcp"], "snowflake"),
            (["aws"], ["gcp"], "aws"),
        ],
    )
    def test_choice(self, exposes: List[str], runtimes: List[str], expected: str) -> None:
        contract = _contract(
            [_expose(f"e{i}", p) for i, p in enumerate(exposes)],
            [_sql(f"b{i}", [], p) for i, p in enumerate(runtimes)],
        )
        assert _simple_apply_provider(contract, LOG) == expected

    def test_a_provider_the_plugin_policy_removed_is_left_to_build_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Blocked by policy, gcp is absent from the registry like a platform no
        # provider serves; build_provider says which it is, not this check.
        from fluid_build import providers as registry
        from fluid_build.cli._common import build_provider

        registry.discover_providers(LOG)
        monkeypatch.setenv("FLUID_PLUGINS_BLOCKLIST", "gcp")
        monkeypatch.delitem(registry.PROVIDERS, "gcp", raising=False)
        monkeypatch.setattr(registry, "POLICY_BLOCKED", {"gcp"})
        name = _simple_apply_provider(_contract([_expose("t", "gcp")]), LOG)
        assert name == "gcp"
        with pytest.raises(CLIError) as exc:
            build_provider(name, None, None, LOG)
        assert exc.value.event == "provider_blocked_by_policy"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FLUID_ENV", raising=False)
    monkeypatch.delenv("FLUID_PROVIDER", raising=False)
    return tmp_path


def _apply(contract: Dict[str, Any], *flags: str) -> int:
    Path("contract.fluid.yaml").write_text(yaml.safe_dump(contract, sort_keys=False), "utf-8")
    args = build_parser().parse_args(["apply", "contract.fluid.yaml", *flags, "--yes"])
    return apply_run(args, LOG)


def _landed(root: Path) -> List[str]:
    return sorted(p.name for p in root.rglob("*") if p.suffix in (".csv", ".parquet"))


class TestFluidApply:
    """The same, end to end through ``fluid apply`` (simple mode, no mocks)."""

    @pytest.mark.parametrize("shape", ["engine", "language"])
    def test_the_731_flink_contract_applies_and_lands_nothing(
        self, workspace: Path, shape: str
    ) -> None:
        contract = _contract([_expose("events_out", "kafka")], [_flink(None, shape=shape)])
        if shape == "language":
            contract["fluidVersion"] = "0.7.5"
        assert _apply(contract) == 0
        assert _landed(workspace) == []

    def test_a_kafka_topic_nothing_writes_is_refused_and_nothing_is_written(
        self, workspace: Path
    ) -> None:
        # Not landed as a placeholder ``id,value / 1,materialized`` CSV.
        contract = _contract([_expose("events_out", "kafka")])
        with pytest.raises(CLIError) as exc:
            _apply(contract)
        assert exc.value.event == "apply_platform_unsupported"
        assert _landed(workspace) == []

    def test_provider_still_overrides(self, workspace: Path) -> None:
        contract = _contract([_expose("events_out", "kafka")])
        assert _apply(copy.deepcopy(contract), "--provider", "local") == 0
