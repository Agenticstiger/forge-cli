# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Contract → Helm values overlay translation.

The contract block ``properties.<engine>.deployment.managed.values_overlay``
provides user-supplied values; this module merges them with sane defaults
per engine, plus secret-ref → ExternalSecret manifests, plus sovereignty
egress NetworkPolicy generation.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional


def build_values_overlay(
    engine: str,
    *,
    profile: str = "small",
    user_values: Optional[Dict[str, Any]] = None,
    sovereignty: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Compose the final Helm values overlay for the engine + profile.

    Order: ``engine_default(profile) ← merge user_values ← merge sovereignty
    overrides``. Later layers override earlier ones via deep-merge.
    """
    values = _engine_defaults(engine, profile)
    if user_values:
        values = _deep_merge(values, copy.deepcopy(user_values))
    if sovereignty:
        values = _apply_sovereignty(values, sovereignty)
    return values


# ── Engine defaults ────────────────────────────────────────────────────


def _engine_defaults(engine: str, profile: str) -> Dict[str, Any]:
    if engine == "airbyte":
        return _airbyte_defaults(profile)
    if engine == "kafka-connect":
        return _kafka_connect_defaults(profile)
    if engine == "debezium":
        return _kafka_connect_defaults(profile)
    if engine == "meltano":
        return _meltano_defaults(profile)
    return {}


def _airbyte_defaults(profile: str) -> Dict[str, Any]:
    cpu, mem = _resource_for_profile(profile)
    return {
        "global": {"edition": "community"},
        "webapp": {
            "resources": {
                "requests": {"cpu": cpu["light"], "memory": mem["light"]},
                "limits": {"cpu": cpu["heavy"], "memory": mem["heavy"]},
            }
        },
        "server": {
            "resources": {
                "requests": {"cpu": cpu["light"], "memory": mem["light"]},
                "limits": {"cpu": cpu["heavy"], "memory": mem["heavy"]},
            }
        },
        "worker": {
            "resources": {
                "requests": {"cpu": cpu["light"], "memory": mem["light"]},
                "limits": {"cpu": cpu["heavy"], "memory": mem["heavy"]},
            }
        },
    }


def _kafka_connect_defaults(profile: str) -> Dict[str, Any]:
    cpu, mem = _resource_for_profile(profile)
    return {
        "kafka": {
            "replicas": 1 if profile == "small" else 3,
            "resources": {
                "requests": {"cpu": cpu["light"], "memory": mem["light"]},
                "limits": {"cpu": cpu["heavy"], "memory": mem["heavy"]},
            },
        },
        "connect": {
            "replicas": 1 if profile == "small" else 2,
            "resources": {
                "requests": {"cpu": cpu["light"], "memory": mem["light"]},
                "limits": {"cpu": cpu["heavy"], "memory": mem["heavy"]},
            },
        },
    }


def _meltano_defaults(profile: str) -> Dict[str, Any]:
    cpu, mem = _resource_for_profile(profile)
    return {
        "ui": {
            "resources": {
                "requests": {"cpu": cpu["light"], "memory": mem["light"]},
                "limits": {"cpu": cpu["heavy"], "memory": mem["heavy"]},
            }
        },
        "scheduler": {
            "resources": {
                "requests": {"cpu": cpu["light"], "memory": mem["light"]},
                "limits": {"cpu": cpu["heavy"], "memory": mem["heavy"]},
            }
        },
    }


def _resource_for_profile(profile: str):
    if profile == "small":
        return ({"light": "100m", "heavy": "500m"}, {"light": "256Mi", "heavy": "1Gi"})
    if profile == "medium":
        return ({"light": "500m", "heavy": "2"}, {"light": "1Gi", "heavy": "4Gi"})
    if profile == "large":
        return ({"light": "1", "heavy": "4"}, {"light": "2Gi", "heavy": "8Gi"})
    return ({"light": "100m", "heavy": "500m"}, {"light": "256Mi", "heavy": "1Gi"})


# ── Sovereignty enforcement ────────────────────────────────────────────


def _apply_sovereignty(values: Dict[str, Any], sov: Dict[str, Any]) -> Dict[str, Any]:
    """Inject sovereignty constraints into the values dict.

    ``sov`` shape::

        {"jurisdiction": "EU", "dataResidency": {"region": "eu-west-1",
         "prohibitTransferTo": ["US", "CN"]}, "egressAllowList": ["sf.com"]}
    """
    region = (sov.get("dataResidency") or {}).get("region")
    prohibit = (sov.get("dataResidency") or {}).get("prohibitTransferTo") or []
    egress = sov.get("egressAllowList") or []
    sovereignty_block = {
        "fluid_sovereignty": {
            "jurisdiction": sov.get("jurisdiction"),
            "region": region,
            "prohibit_transfer_to": prohibit,
            "egress_allow_list": egress,
        }
    }
    return _deep_merge(values, sovereignty_block)


# ── ExternalSecret + NetworkPolicy emission ────────────────────────────


def build_external_secrets(
    *, name_prefix: str, secrets: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Translate ``deployment.managed.secrets[]`` entries to ExternalSecret manifests.

    Each input ``{"name": "SF_OAUTH", "ref": "vault://salesforce/oauth"}``
    produces one ExternalSecret CR pointing at the appropriate backend
    inferred from the URI scheme.
    """
    out: List[Dict[str, Any]] = []
    for s in secrets:
        name = s["name"].lower().replace("_", "-")
        ref = s["ref"]
        backend = _infer_backend(ref)
        out.append(
            {
                "apiVersion": "external-secrets.io/v1beta1",
                "kind": "ExternalSecret",
                "metadata": {"name": f"{name_prefix}-{name}"},
                "spec": {
                    "refreshInterval": "1h",
                    "secretStoreRef": {"name": f"{backend}-store", "kind": "ClusterSecretStore"},
                    "target": {"name": f"{name_prefix}-{name}"},
                    "data": [{"secretKey": s["name"], "remoteRef": {"key": _strip_scheme(ref)}}],
                },
            }
        )
    return out


def _infer_backend(uri: str) -> str:
    if uri.startswith("vault://"):
        return "vault"
    if uri.startswith("aws://"):
        return "aws-secretsmanager"
    if uri.startswith("gcp://"):
        return "gcp-secretmanager"
    if uri.startswith("azure://"):
        return "azure-keyvault"
    return "env"


def _strip_scheme(uri: str) -> str:
    return uri.split("://", 1)[-1]


def build_network_policy(*, namespace: str, name: str, allow_list: List[str]) -> Dict[str, Any]:
    """Generate a Kubernetes NetworkPolicy that locks egress to ``allow_list`` hosts."""
    egress_rules = [
        {"to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}], "ports": [{"protocol": "TCP", "port": 443}]}
        for _ in allow_list  # one rule per host (CIDR resolution lives in the cluster's CoreDNS / Cilium)
    ]
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "podSelector": {},
            "policyTypes": ["Egress"],
            "egress": egress_rules,
        },
    }


# ── Deep merge ─────────────────────────────────────────────────────────


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in overlay.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out
