# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Alert routes through the real ``fluid`` entry point: ``fluid validate`` checks them,
``fluid generate schedule`` writes them into the DAGs, and an organisation's pack, installed as
a distribution, adds its default routes through ``fluid_build.alert_defaults``."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from tests.cli._schedule_dag_fixtures import DEMO_CONTRACT, DEMO_CONTRACT_PATH

REPO = Path(__file__).resolve().parents[2]
MINIMAL_077 = REPO / "tests/fixtures/contracts/compatibility/minimal_077.yaml"

#: A Zenduty integration URL, pasted where the connection id belongs.
PASTED = (
    "https://events.zenduty.com/integration/ab12cd34/generic/" + "0f8fad5b-d9cb-469f-a165-"
    "70867728950e/"
)

ROUTES: List[Dict[str, Any]] = [
    {
        "name": "oncall",
        "kind": "webhook",
        "format": "zenduty",
        "connection": "zenduty_oncall",
        "events": ["failure", "success", "sla_miss"],
    },
    {"name": "chat", "kind": "slack", "connection": "slack_data_alerts"},
]


def _fluid(
    cwd: Path, *argv: str, site: Optional[Path] = None
) -> "subprocess.CompletedProcess[str]":
    env = {k: v for k, v in os.environ.items() if not k.startswith("FLUID_PLUGINS_")}
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(REPO), str(site or "")) if p)
    env["COLUMNS"] = "200"
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    return subprocess.run(
        [sys.executable, "-m", "fluid_build.cli", *argv],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _validate_contract(tmp_path: Path, alerts: Dict[str, Any]) -> Path:
    doc = yaml.safe_load(MINIMAL_077.read_text(encoding="utf-8"))
    doc["alerts"] = alerts
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def _scheduled_project(tmp_path: Path, alerts: Optional[Dict[str, Any]]) -> Path:
    doc = yaml.safe_load(DEMO_CONTRACT)
    doc["fluidVersion"] = "0.7.7"
    if alerts is not None:
        doc["alerts"] = alerts
    path = tmp_path / DEMO_CONTRACT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def _org_pack(root: Path, routes: Any) -> Path:
    """A site directory holding an installed ``acme-handoff-pack`` whose
    ``fluid_build.alert_defaults`` entry point returns ``routes``."""
    site = root / "site"
    dist = site / "acme_handoff_pack-1.0.dist-info"
    dist.mkdir(parents=True)
    (dist / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: acme-handoff-pack\nVersion: 1.0\n", encoding="utf-8"
    )
    (dist / "entry_points.txt").write_text(
        "[fluid_build.alert_defaults]\nacme = acme_handoff_pack:default_routes\n",
        encoding="utf-8",
    )
    (site / "acme_handoff_pack.py").write_text(
        f"def default_routes():\n    return {routes!r}\n", encoding="utf-8"
    )
    return site


class TestValidate:
    def test_routes_that_render_pass(self, tmp_path: Path) -> None:
        contract = _validate_contract(tmp_path, {"sla": "PT4H", "routes": ROUTES})
        run = _fluid(tmp_path, "validate", str(contract), "--offline")
        assert run.returncode == 0, run.stdout + run.stderr

    def test_a_pasted_url_fails_and_is_never_printed(self, tmp_path: Path) -> None:
        routes = [{**ROUTES[0], "connection": PASTED}]
        contract = _validate_contract(tmp_path, {"sla": "PT4H", "routes": routes})
        run = _fluid(tmp_path, "validate", str(contract), "--offline", "--format", "json")
        assert run.returncode == 1
        output = run.stdout + run.stderr
        assert PASTED not in output and "70867728950e" not in output and "ab12cd34" not in output
        report = json.loads(run.stdout[run.stdout.index("{") :])
        alert_issues = [i for i in report["issues"] if i.get("code") == "ERR_CONTRACT_ALERTS"]
        assert alert_issues and alert_issues[0]["pointer"] == "/alerts/routes/0/connection"
        assert "(value not shown)" in alert_issues[0]["message"]

    def test_the_org_packs_defaults_are_checked(self, tmp_path: Path) -> None:
        site = _org_pack(tmp_path, [{"name": "oncall", "kind": "webhook"}])
        contract = _validate_contract(tmp_path, {"routes": []})
        run = _fluid(tmp_path, "validate", str(contract), "--offline", site=site)
        assert run.returncode == 1
        assert "connection is required for a webhook route" in run.stdout


class TestGenerateSchedule:
    def _generate(self, tmp_path: Path, site: Optional[Path] = None) -> Dict[str, str]:
        run = _fluid(tmp_path, "generate", "schedule", DEMO_CONTRACT_PATH, "-o", "out", site=site)
        assert run.returncode == 0, run.stdout + run.stderr
        return {p.name: p.read_text(encoding="utf-8") for p in (tmp_path / "out").glob("*.py")}

    def test_the_dag_carries_the_routes(self, tmp_path: Path) -> None:
        _scheduled_project(tmp_path, {"sla": "PT4H", "routes": ROUTES})
        (source,) = self._generate(tmp_path).values()
        ast.parse(source)
        assert "from airflow.providers.http.notifications.http import HttpNotifier" in source
        assert "SlackWebhookNotifier" in source and "**ALERT_TASK_ARGS," in source
        assert "http_conn_id='zenduty_oncall'" in source
        assert "DeadlineAlert(" in source and 'ALERT_DAG_ARGS["sla_miss_callback"]' in source

    def test_an_org_packs_default_route_reaches_every_dag(self, tmp_path: Path) -> None:
        default = {"name": "org-oncall", "kind": "pagerduty", "connection": "pd_events"}
        site = _org_pack(tmp_path, [default])
        _scheduled_project(tmp_path, None)
        (source,) = self._generate(tmp_path, site=site).values()
        assert "PagerdutyNotifier" in source and "pagerduty_events_conn_id='pd_events'" in source
        assert "the default routes of acme" in " ".join(source.split())

    def test_a_contract_can_leave_the_defaults_out(self, tmp_path: Path) -> None:
        site = _org_pack(tmp_path, [{"name": "org-oncall", "kind": "pagerduty"}])
        _scheduled_project(tmp_path, {"defaults": False, "routes": []})
        (source,) = self._generate(tmp_path, site=site).values()
        assert "ALERT_" not in source

    def test_a_blocked_org_pack_adds_nothing(self, tmp_path: Path, monkeypatch: Any) -> None:
        site = _org_pack(tmp_path, [{"name": "org-oncall", "kind": "pagerduty"}])
        _scheduled_project(tmp_path, None)
        monkeypatch.setenv("FLUID_PLUGINS_BLOCKLIST", "acme")
        env_run = subprocess.run(
            [sys.executable, "-m", "fluid_build.cli", "generate", "schedule"]
            + [DEMO_CONTRACT_PATH, "-o", "out"],
            cwd=tmp_path,
            env={
                **{k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"},
                "PYTHONPATH": os.pathsep.join((str(REPO), str(site))),
                "FLUID_PLUGINS_BLOCKLIST": "acme",
            },
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert env_run.returncode == 0, env_run.stdout + env_run.stderr
        (source,) = [p.read_text() for p in (tmp_path / "out").glob("*.py")]
        assert "ALERT_" not in source

    def test_a_bad_route_refuses_the_dag_without_quoting_it(self, tmp_path: Path) -> None:
        _scheduled_project(tmp_path, {"routes": [{**ROUTES[1], "connection": PASTED}]})
        run = _fluid(tmp_path, "generate", "schedule", DEMO_CONTRACT_PATH, "-o", "out")
        assert run.returncode == 2
        output = run.stdout + run.stderr
        assert "alerts.routes[0].connection" in output and PASTED not in output
        assert not list((tmp_path / "out").glob("*.py"))
