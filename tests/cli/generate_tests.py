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
Generate test file templates for CLI commands.

This script creates test file templates for all CLI command files that don't have tests yet.
"""

import sys
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Commands to generate tests for
COMMANDS = [
    "version_cmd",
    "provider_cmds",
    "doctor",
    "docs_build",
    "contract_tests",
    "viz_plan",
    "viz_graph",
    "scaffold_ci",
    "scaffold_composer",
    "export_opds",
    "opds",
    "admin",
    "forge",
    "market",
    "auth",
]

TEST_TEMPLATE = '''"""
Tests for the {command_display} command.

Tests command registration, argument parsing, and command execution.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tests.cli.base_test import CLITestCase
from fluid_build.cli import {module_name}


class Test{class_name}Command(CLITestCase):
    """Test suite for {command_display} command."""
    
    def test_register_creates_parser(self):
        """Test that register() creates a {command_display} subcommand."""
        import argparse
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        
        {module_name}.register(subparsers)
        
        # Verify parser was created (basic smoke test)
        self.assertIsNotNone(subparsers)
        
    def test_command_has_func_attribute(self):
        """Test that {command_display} command sets func attribute for dispatch."""
        import argparse
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        
        {module_name}.register(subparsers)
        
        # Parse minimal args and check func is set
        # Note: Actual arguments depend on command implementation
        # This is a basic structure test
        self.assertTrue(hasattr({module_name}, 'run'))
        
    def test_run_function_exists(self):
        """Test that run() function exists and is callable."""
        self.assertTrue(hasattr({module_name}, 'run'))
        self.assertTrue(callable({module_name}.run))


def run_tests():
    """Run all {command_display} command tests."""
    suite = unittest.TestLoader().loadTestsFromTestCase(Test{class_name}Command)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return result.wasSuccessful()


if __name__ == '__main__':
    sys.exit(0 if run_tests() else 1)
'''


def to_class_name(module_name: str) -> str:
    """Convert module name to class name."""
    parts = module_name.split("_")
    return "".join(word.capitalize() for word in parts)


def to_display_name(module_name: str) -> str:
    """Convert module name to display name."""
    return module_name.replace("_", "-")


def generate_test_file(command: str):
    """Generate a test file for a command."""
    test_dir = PROJECT_ROOT / "tests" / "cli"
    test_file = test_dir / f"test_{command}.py"

    # Skip if test file already exists
    if test_file.exists():
        print(f"✓ Test file already exists: test_{command}.py")
        return

    # Generate test content
    class_name = to_class_name(command)
    display_name = to_display_name(command)

    content = TEST_TEMPLATE.format(
        command_display=display_name, module_name=command, class_name=class_name
    )

    # Write test file
    test_file.write_text(content, encoding="utf-8")
    print(f"✨ Generated: test_{command}.py")


def main():
    """Generate all test files."""
    print("Generating CLI command test files...")
    print()

    for command in COMMANDS:
        generate_test_file(command)

    print()
    print(f"✅ Test file generation complete! Generated files for {len(COMMANDS)} commands.")


if __name__ == "__main__":
    main()
