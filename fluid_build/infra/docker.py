# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Docker Compose artifact generator for managed-mode acquisition deployments.

Emits a deterministic ``docker-compose.yaml`` plus a ``.env`` template that
brings up the engine's stack locally for dev / CI. The compose file is
shaped per engine (Airbyte / Kafka Connect / Meltano).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import yaml

from .base import ArtifactBundle, ArtifactGenerator, InfraStatus, InfraValidationResult, make_file


@dataclass
class DockerComposeGenerator(ArtifactGenerator):
    target: str = "docker"

    def generate(
        self,
        contract: Dict[str, Any],
        *,
        env: Optional[Dict[str, str]] = None,
    ) -> ArtifactBundle:
        builds = contract.get("builds") or []
        engines = sorted({b.get("engine") for b in builds if b.get("pattern") == "acquisition"})
        services: Dict[str, Any] = {}
        for engine in engines:
            services.update(_services_for_engine(engine, contract))
        compose = {"services": services} if services else {"services": {}}
        compose_yaml = yaml.safe_dump(compose, sort_keys=True, default_flow_style=False)
        env_template = _env_template_for(contract)
        files = [
            make_file("docker-compose.yaml", compose_yaml),
            make_file(".env.template", env_template),
        ]
        return ArtifactBundle.of(
            "docker",
            files,
            metadata={"engines": list(engines), "service_count": len(services)},
        )

    def validate(self, bundle: ArtifactBundle) -> InfraValidationResult:
        # Local validation: compose YAML must parse + every service must have
        # an image. Schema check is deliberately strict to catch typos early.
        errors: List[str] = []
        warnings: List[str] = []
        for f in bundle.files:
            if not f.relative_path.endswith(("yaml", "yml")):
                continue
            try:
                doc = yaml.safe_load(f.content)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{f.relative_path}: invalid YAML: {exc}")
                continue
            services = (doc or {}).get("services") or {}
            for name, svc in services.items():
                if not svc.get("image"):
                    errors.append(f"{f.relative_path}: service '{name}' missing 'image'")
        return InfraValidationResult(ok=not errors, errors=errors, warnings=warnings)

    def status(self, contract: Dict[str, Any]) -> InfraStatus:
        # Status reporting against `docker compose ps` lives in the apply
        # path (subprocess shim); the generator doesn't probe live state.
        return InfraStatus(deployed=False, notes=["docker status check belongs to apply layer"])


# ── Per-engine service templates ───────────────────────────────────────


def _services_for_engine(engine: Optional[str], contract: Dict[str, Any]) -> Dict[str, Any]:
    if engine is None:
        return {}
    if engine == "airbyte":
        return _airbyte_services()
    if engine == "kafka-connect" or engine == "debezium":
        return _kafka_connect_services()
    if engine == "meltano":
        return _meltano_services()
    if engine == "duckdb":
        return {}  # zero-infra
    if engine == "dlt":
        return {}  # zero-infra
    return {}


def _airbyte_services() -> Dict[str, Any]:
    return {
        "airbyte-db": {
            "image": "postgres:16-alpine",
            "environment": {
                "POSTGRES_DB": "airbyte",
                "POSTGRES_USER": "airbyte",
                "POSTGRES_PASSWORD": "${AIRBYTE_DB_PASSWORD:-airbyte}",
            },
            "volumes": ["airbyte-db-data:/var/lib/postgresql/data"],
            "healthcheck": {
                "test": ["CMD-SHELL", "pg_isready -U airbyte -d airbyte"],
                "interval": "5s",
                "retries": 30,
            },
        },
        "airbyte-server": {
            "image": "airbyte/server:0.50.30",
            "depends_on": {"airbyte-db": {"condition": "service_healthy"}},
            "environment": {
                "DATABASE_URL": "jdbc:postgresql://airbyte-db:5432/airbyte",
                "DATABASE_USER": "airbyte",
                "DATABASE_PASSWORD": "${AIRBYTE_DB_PASSWORD:-airbyte}",
            },
            "ports": ["8001:8001"],
        },
        "airbyte-webapp": {
            "image": "airbyte/webapp:0.50.30",
            "depends_on": ["airbyte-server"],
            "ports": ["8000:80"],
            "environment": {"INTERNAL_API_HOST": "airbyte-server:8001"},
        },
    }


def _kafka_connect_services() -> Dict[str, Any]:
    return {
        "zookeeper": {
            "image": "confluentinc/cp-zookeeper:7.6.0",
            "environment": {
                "ZOOKEEPER_CLIENT_PORT": "2181",
                "ZOOKEEPER_TICK_TIME": "2000",
            },
        },
        "kafka": {
            "image": "confluentinc/cp-kafka:7.6.0",
            "depends_on": ["zookeeper"],
            "ports": ["9092:9092"],
            "environment": {
                "KAFKA_BROKER_ID": "1",
                "KAFKA_ZOOKEEPER_CONNECT": "zookeeper:2181",
                "KAFKA_ADVERTISED_LISTENERS": "PLAINTEXT://kafka:9092",
                "KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR": "1",
            },
        },
        "kafka-connect": {
            "image": "confluentinc/cp-kafka-connect:7.6.0",
            "depends_on": ["kafka"],
            "ports": ["8083:8083"],
            "environment": {
                "CONNECT_BOOTSTRAP_SERVERS": "kafka:9092",
                "CONNECT_REST_PORT": "8083",
                "CONNECT_GROUP_ID": "fluid-acquire",
                "CONNECT_CONFIG_STORAGE_TOPIC": "_connect-configs",
                "CONNECT_OFFSET_STORAGE_TOPIC": "_connect-offsets",
                "CONNECT_STATUS_STORAGE_TOPIC": "_connect-status",
                "CONNECT_KEY_CONVERTER": "org.apache.kafka.connect.json.JsonConverter",
                "CONNECT_VALUE_CONVERTER": "org.apache.kafka.connect.json.JsonConverter",
                "CONNECT_PLUGIN_PATH": "/usr/share/java,/usr/share/confluent-hub-components",
            },
        },
    }


def _meltano_services() -> Dict[str, Any]:
    return {
        "meltano": {
            "image": "meltano/meltano:latest",
            "ports": ["5000:5000"],
            "command": ["ui"],
            "volumes": ["./meltano-project:/project"],
            "working_dir": "/project",
        }
    }


def _env_template_for(contract: Dict[str, Any]) -> str:
    """Generate a `.env.template` listing all secret refs used by the contract."""
    refs: List[str] = []
    for build in contract.get("builds") or []:
        if build.get("pattern") != "acquisition":
            continue
        props = build.get("properties") or {}
        connection = (props.get("source") or {}).get("connection") or {}
        secret_ref = connection.get("secretRef")
        if secret_ref:
            refs.append(f"# secretRef: {secret_ref}\n# Set the corresponding env var below:")
        for engine_block in ("airbyte", "kafka-connect", "debezium", "meltano", "dlt", "duckdb"):
            block = props.get(engine_block) or {}
            deployment = block.get("deployment") or {}
            managed = deployment.get("managed") or {}
            for s in managed.get("secrets") or []:
                refs.append(f"{s['name']}=  # ref: {s['ref']}")
    if not refs:
        refs = ["# No secrets required by this contract"]
    return "\n".join(refs) + "\n"
