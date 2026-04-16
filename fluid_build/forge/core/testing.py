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

"""
Testing framework for FLUID Forge templates and providers

This module provides comprehensive testing utilities to validate that:
1. Templates generate valid project structures
2. Generated projects can be built and run successfully
3. FLUID contracts are valid and complete
4. Providers generate correct deployment configurations
"""

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple

from fluid_build.cli.console import cprint

try:
    import yaml
except ImportError:
    # Fallback YAML implementation
    import json

    class yaml:
        @staticmethod
        def safe_load(f):
            return json.load(f)

        @staticmethod
        def dump(data, f, **kwargs):
            json.dump(data, f, indent=2)


from ..core.interfaces import GenerationContext, ProjectTemplate
from ..core.registry import get_provider_registry, get_template_registry


@dataclass
class TestResult:
    """Result of a template test"""

    success: bool
    errors: List[str]
    warnings: List[str]
    generated_files: List[str]
    execution_time: float


class TemplateTestSuite:
    """Comprehensive test suite for project templates"""

    def __init__(self, template_name: str):
        self.template_name = template_name
        self.template_registry = get_template_registry()
        self.provider_registry = get_provider_registry()

    def run_full_test(self, config: Dict[str, Any]) -> TestResult:
        """Run comprehensive test of template"""
        import time

        start_time = time.time()

        errors = []
        warnings = []
        generated_files = []

        try:
            # Get template
            template_class = self.template_registry.get(self.template_name)
            if not template_class:
                return TestResult(
                    success=False,
                    errors=[f"Template '{self.template_name}' not found"],
                    warnings=[],
                    generated_files=[],
                    execution_time=0.0,
                )

            template = template_class()

            # Test 1: Validate template metadata
            metadata_errors = self._test_metadata(template)
            errors.extend(metadata_errors)

            # Test 2: Generate project in temporary directory
            with tempfile.TemporaryDirectory() as temp_dir:
                target_path = Path(temp_dir) / "test_project"

                generation_errors, files = self._test_generation(template, config, target_path)
                errors.extend(generation_errors)
                generated_files = files

                if not errors:
                    # Test 3: Validate generated files
                    validation_errors = self._test_file_validation(target_path)
                    errors.extend(validation_errors)

                    # Test 4: Validate FLUID contract
                    contract_errors = self._test_contract_validation(target_path)
                    errors.extend(contract_errors)

                    # Test 5: Test project can be built
                    build_errors, build_warnings = self._test_project_build(target_path)
                    errors.extend(build_errors)
                    warnings.extend(build_warnings)

        except Exception as e:
            errors.append(f"Unexpected error during testing: {str(e)}")

        execution_time = time.time() - start_time

        return TestResult(
            success=len(errors) == 0,
            errors=errors,
            warnings=warnings,
            generated_files=generated_files,
            execution_time=execution_time,
        )

    def _test_metadata(self, template: ProjectTemplate) -> List[str]:
        """Test template metadata is valid"""
        errors = []

        try:
            metadata = template.get_metadata()

            # Check required fields
            if not metadata.name:
                errors.append("Template metadata missing name")

            if not metadata.description:
                errors.append("Template metadata missing description")

            if not metadata.provider_support:
                errors.append("Template metadata missing provider support")

            if not metadata.use_cases:
                errors.append("Template metadata missing use cases")

            # Check complexity is valid
            if metadata.complexity not in ["beginner", "intermediate", "advanced"]:
                errors.append(f"Invalid complexity level: {metadata.complexity}")

        except Exception as e:
            errors.append(f"Error getting template metadata: {str(e)}")

        return errors

    def _test_generation(
        self, template: ProjectTemplate, config: Dict[str, Any], target_path: Path
    ) -> Tuple[List[str], List[str]]:
        """Test project generation"""
        errors = []
        generated_files = []

        try:
            # Create generation context
            context = GenerationContext(
                project_config=config,
                target_dir=target_path,
                template_metadata=template.get_metadata(),
                provider_config=config.get("provider_config", {}),
                user_selections=config.get("user_selections", {}),
                forge_version="test",
                creation_time="2025-10-14",
            )

            # Generate project structure
            structure = template.generate_structure(context)

            # Create directories and files
            target_path.mkdir(parents=True, exist_ok=True)
            generated_files = self._create_project_structure(target_path, structure)

            # Generate FLUID contract
            contract = template.generate_contract(context)
            contract_path = target_path / "contract.fluid.yaml"

            with open(contract_path, "w") as f:
                yaml.dump(contract, f, default_flow_style=False)

            generated_files.append(str(contract_path))

        except Exception as e:
            errors.append(f"Error during project generation: {str(e)}")

        return errors, generated_files

    def _create_project_structure(self, base_path: Path, structure: Dict[str, Any]) -> List[str]:
        """Create project structure from template definition"""
        created_files = []

        for item_name, item_content in structure.items():
            item_path = base_path / item_name

            if isinstance(item_content, dict):
                # Directory with contents
                item_path.mkdir(parents=True, exist_ok=True)
                if item_content:  # Not empty
                    sub_files = self._create_project_structure(item_path, item_content)
                    created_files.extend(sub_files)

            elif isinstance(item_content, str):
                # File with content
                item_path.parent.mkdir(parents=True, exist_ok=True)
                with open(item_path, "w") as f:
                    f.write(item_content)
                created_files.append(str(item_path))

            elif isinstance(item_content, list) and not item_content:
                # Empty directory
                item_path.mkdir(parents=True, exist_ok=True)

        return created_files

    def _test_file_validation(self, project_path: Path) -> List[str]:
        """Validate generated files"""
        errors = []

        # Check required files exist
        required_files = ["contract.fluid.yaml", "README.md", "requirements.txt"]

        for required_file in required_files:
            file_path = project_path / required_file
            if not file_path.exists():
                errors.append(f"Required file missing: {required_file}")

        # Check Python files are valid syntax
        for py_file in project_path.rglob("*.py"):
            try:
                with open(py_file) as f:
                    compile(f.read(), str(py_file), "exec")
            except SyntaxError as e:
                errors.append(f"Python syntax error in {py_file}: {str(e)}")

        # Check YAML files are valid
        for yaml_file in project_path.rglob("*.yaml"):
            try:
                with open(yaml_file) as f:
                    yaml.safe_load(f)
            except yaml.YAMLError as e:
                errors.append(f"YAML syntax error in {yaml_file}: {str(e)}")

        return errors

    def _test_contract_validation(self, project_path: Path) -> List[str]:
        """Validate FLUID contract"""
        errors = []

        contract_path = project_path / "contract.fluid.yaml"
        if not contract_path.exists():
            return ["FLUID contract file not found"]

        try:
            with open(contract_path) as f:
                contract = yaml.safe_load(f)

            # Check required contract fields
            required_fields = ["apiVersion", "kind", "metadata", "spec"]
            for field in required_fields:
                if field not in contract:
                    errors.append(f"Contract missing required field: {field}")

            # Check metadata
            if "metadata" in contract:
                metadata = contract["metadata"]
                if "name" not in metadata:
                    errors.append("Contract metadata missing name")

            # Check spec structure
            if "spec" in contract:
                spec = contract["spec"]
                if "inputs" not in spec and "outputs" not in spec:
                    errors.append("Contract spec must have inputs or outputs")

        except Exception as e:
            errors.append(f"Error validating contract: {str(e)}")

        return errors

    def _test_project_build(self, project_path: Path) -> Tuple[List[str], List[str]]:
        """Test that generated project can be built"""
        errors = []
        warnings = []

        # Check if requirements.txt exists and install dependencies
        requirements_path = project_path / "requirements.txt"
        if requirements_path.exists():
            try:
                # Test pip install --dry-run to check dependencies
                result = subprocess.run(
                    ["pip", "install", "--dry-run", "-r", str(requirements_path)],
                    capture_output=True,
                    text=True,
                    cwd=project_path,
                )

                if result.returncode != 0:
                    warnings.append(f"Dependencies may have issues: {result.stderr}")

            except Exception as e:
                warnings.append(f"Could not test dependencies: {str(e)}")

        # Test Python imports
        for py_file in project_path.rglob("*.py"):
            if py_file.name.startswith("__"):
                continue

            try:
                # Simple import test
                result = subprocess.run(
                    ["python", "-m", "py_compile", str(py_file)],
                    capture_output=True,
                    text=True,
                    cwd=project_path,
                )

                if result.returncode != 0:
                    errors.append(f"Python compilation failed for {py_file}: {result.stderr}")

            except Exception as e:
                warnings.append(f"Could not test Python file {py_file}: {str(e)}")

        return errors, warnings


class ForgeTestRunner:
    """Main test runner for the forge system"""

    def __init__(self):
        self.template_registry = get_template_registry()
        self.provider_registry = get_provider_registry()

    def run_all_template_tests(self) -> Dict[str, TestResult]:
        """Run tests for all registered templates"""
        results = {}

        for template_name in self.template_registry.list_available():
            cprint(f"Testing template: {template_name}")

            # Create test configuration
            config = {
                "name": f"test_{template_name}",
                "description": f"Test project for {template_name} template",
                "owner": "test-team",
                "domain": "testing",
                "provider": "local",
            }

            # Run test
            test_suite = TemplateTestSuite(template_name)
            result = test_suite.run_full_test(config)
            results[template_name] = result

            # Print summary
            status = "✅ PASS" if result.success else "❌ FAIL"
            cprint(f"  {status} ({result.execution_time:.2f}s)")

            if result.errors:
                for error in result.errors:
                    cprint(f"    Error: {error}")

            if result.warnings:
                for warning in result.warnings:
                    cprint(f"    Warning: {warning}")

        return results

    def generate_test_report(self, results: Dict[str, TestResult]) -> str:
        """Generate a comprehensive test report"""

        total_tests = len(results)
        passed_tests = sum(1 for result in results.values() if result.success)

        report = f"""
# FLUID Forge Template Test Report

## Summary
- **Total Templates Tested**: {total_tests}
- **Passed**: {passed_tests}
- **Failed**: {total_tests - passed_tests}
- **Success Rate**: {passed_tests/total_tests*100:.1f}%

## Detailed Results

"""

        for template_name, result in results.items():
            status = "✅ PASS" if result.success else "❌ FAIL"
            report += f"### {template_name} {status}\n"
            report += f"- **Execution Time**: {result.execution_time:.2f}s\n"
            report += f"- **Generated Files**: {len(result.generated_files)}\n"

            if result.errors:
                report += "- **Errors**:\n"
                for error in result.errors:
                    report += f"  - {error}\n"

            if result.warnings:
                report += "- **Warnings**:\n"
                for warning in result.warnings:
                    report += f"  - {warning}\n"

            report += "\n"

        return report


# Convenience function for running tests
def test_all_templates():
    """Run tests for all templates and print results"""
    runner = ForgeTestRunner()
    results = runner.run_all_template_tests()

    cprint("\n" + "=" * 50)
    cprint("FORGE TEMPLATE TEST SUMMARY")
    cprint("=" * 50)

    total = len(results)
    passed = sum(1 for r in results.values() if r.success)

    cprint(f"Total: {total}, Passed: {passed}, Failed: {total-passed}")
    cprint(f"Success Rate: {passed/total*100:.1f}%")

    return results


if __name__ == "__main__":
    test_all_templates()
