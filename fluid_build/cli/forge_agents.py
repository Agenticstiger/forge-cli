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

"""Compatibility surface for Forge domain agents."""

from __future__ import annotations

from typing import Dict, List

from .forge_domain_agent_base import (
    AIAgentBase,
    DeclarativeDomainAgent,
    _choice_label,
    _raw_answer,
    _resolve_context_choice,
)


class _SpecBackedDomainAgent(DeclarativeDomainAgent):
    """Shared declarative domain-agent binding for named compatibility classes."""

    spec_name = ""

    def __init__(self) -> None:
        super().__init__(self.spec_name)


class FinanceAgent(_SpecBackedDomainAgent):
    """Finance and banking domain expert."""

    spec_name = "finance"


class HealthcareAgent(_SpecBackedDomainAgent):
    """Healthcare and life sciences domain expert."""

    spec_name = "healthcare"


class RetailAgent(_SpecBackedDomainAgent):
    """Retail and e-commerce domain expert."""

    spec_name = "retail"


class TelcoAgent(_SpecBackedDomainAgent):
    """TM Forum SID-aligned telecom domain agent."""

    spec_name = "telco"


DOMAIN_AGENTS = {
    "finance": FinanceAgent,
    "healthcare": HealthcareAgent,
    "retail": RetailAgent,
    "telco": TelcoAgent,
}


def get_agent(agent_name: str) -> AIAgentBase:
    """Get a domain agent by name (user-defined agents are checked first)."""
    # Built-in agents.
    if agent_name in DOMAIN_AGENTS:
        return DOMAIN_AGENTS[agent_name]()

    # User-defined agents (workspace → global).
    try:
        from fluid_build.cli.forge_agent_specs import load_user_or_builtin_spec

        spec = load_user_or_builtin_spec(agent_name)
        return DeclarativeDomainAgent(spec.name)
    except Exception:  # noqa: BLE001
        pass

    available = ", ".join(get_all_domain_names())
    raise ValueError(f"Agent '{agent_name}' not found. Available: {available}")


def get_all_domain_names() -> List[str]:
    """Return all available domain names (built-in + user-defined)."""
    names = list(DOMAIN_AGENTS.keys())
    try:
        from fluid_build.cli.forge_agent_specs import discover_all_agent_specs

        for name in discover_all_agent_specs():
            if name not in names:
                names.append(name)
    except Exception:  # noqa: BLE001
        pass
    return sorted(names)


def list_agents() -> List[Dict[str, str]]:
    """List all available domain agents (built-in + user-defined)."""
    agents = []
    seen = set()
    for name, agent_class in DOMAIN_AGENTS.items():
        agent = agent_class()
        agents.append(
            {"name": agent.name, "domain": agent.domain, "description": agent.description, "source": "built-in"}
        )
        seen.add(agent.name)

    try:
        from fluid_build.cli.forge_agent_specs import discover_all_agent_specs

        for name, spec in discover_all_agent_specs().items():
            if name not in seen:
                agents.append(
                    {"name": spec.name, "domain": spec.domain, "description": spec.description, "source": "user"}
                )
    except Exception:  # noqa: BLE001
        pass

    return agents


__all__ = [
    "AIAgentBase",
    "FinanceAgent",
    "HealthcareAgent",
    "RetailAgent",
    "TelcoAgent",
    "DOMAIN_AGENTS",
    "get_agent",
    "get_all_domain_names",
    "list_agents",
    "_raw_answer",
    "_resolve_context_choice",
    "_choice_label",
]
