# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures: no DNS, no operator environment, and contract builders."""

from __future__ import annotations

import socket

import pytest

OPERATOR_VARS = (
    "FLUID_ACQUISITION_DEPLOYMENT_HOST_ALLOWLIST",
    "FLUID_ACQUISITION_SECRET_REF_ALLOWLIST",
    "FLUID_SECRET_SCHEMES",
    "FLUID_SECRET_PREFIXES",
    "SCHEMA_REGISTRY_REST_ENDPOINT",
)


@pytest.fixture(autouse=True)
def clean_operator_environment(monkeypatch):
    for name in OPERATOR_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def offline_dns(monkeypatch):
    """Every name fails to resolve; an IP literal is itself. No unit test touches DNS."""
    import ipaddress

    def getaddrinfo(host, port, *args, **kwargs):
        text = host.decode() if isinstance(host, bytes) else str(host)
        try:
            ipaddress.ip_address(text.split("%", 1)[0])
        except ValueError:
            raise socket.gaierror(socket.EAI_NONAME, "name resolution is stubbed")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (text, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.fixture
def public_dns(monkeypatch):
    """Every name resolves to one public address."""

    def getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


def consumed_contract(registry, fmt="avro-confluent", topic="orders", columns=None):
    """A contract with one build reading ``topic`` through ``registry``."""
    stream = {
        "kind": "kafka",
        "topic": topic,
        "format": fmt,
        "schema": (
            columns
            if columns is not None
            else [
                {"name": "order_id", "type": "string", "required": True},
                {"name": "amount", "type": "long", "required": True},
            ]
        ),
        "registry": registry,
    }
    return {
        "fluidVersion": "0.7.7",
        "id": "t.consumer",
        "builds": [{"id": "ingest", "properties": {"stream": stream}}],
    }


def produced_contract(registry, platform="kafka", fmt="kafka_topic", serialization="avro"):
    """A contract with one Kafka expose that registers its value schema in ``registry``."""
    return {
        "fluidVersion": "0.7.7",
        "id": "t.producer",
        "exposes": [
            {
                "exposeId": "events",
                "kind": "stream",
                "binding": {
                    "platform": platform,
                    "format": fmt,
                    "location": {"topic": "evt", "host": "b-1.msk.example:9098"},
                    "serialization": {"value": serialization},
                    "properties": {"registry": registry},
                },
                "contract": {
                    "schema": [
                        {"name": "id", "type": "string", "required": True},
                        {"name": "email", "type": "string", "sensitivity": "pii"},
                    ]
                },
            }
        ],
    }
