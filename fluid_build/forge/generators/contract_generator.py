# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""
Contract Generator for FLUID Forge

Generates FLUID contract files with proper schema validation
"""

from typing import Dict

from ..core.interfaces import GenerationContext, Generator, ValidationResult


class ContractGenerator(Generator):
    """Generator for FLUID contract files"""

    def generate(self, context: GenerationContext) -> Dict[str, str]:
        """Generate FLUID contract file"""
        import yaml

        if context.project_config.get("copilot_generated_contract"):
            contract_yaml = yaml.dump(
                context.project_config["copilot_generated_contract"],
                default_flow_style=False,
                sort_keys=False,
            )
            return {"contract.fluid.yaml": contract_yaml}

        # Get template to generate contract
        template_name = context.project_config.get("template")
        if not template_name:
            return {}

        # Use template's contract generation if available
        from ..core.registry import template_registry

        template = template_registry.get(template_name)

        if template:
            contract = template.generate_contract(context)

            # Convert to YAML string
            contract_yaml = yaml.dump(contract, default_flow_style=False, sort_keys=False)

            return {"contract.fluid.yaml": contract_yaml}

        return {}

    def validate_context(self, context: GenerationContext) -> ValidationResult:
        """Validate that context has required data"""
        if not context.project_config.get("template"):
            return False, ["Template is required for contract generation"]
        return True, []
