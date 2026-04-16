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
Pytest Configuration and Fixtures for CLI Tests

Shared fixtures for testing FLUID CLI commands.
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

from fluid_build.cli.core import CLIContext


@pytest.fixture
def temp_dir():
    """Create a temporary directory for test files"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def sample_contract():
    """Sample FLUID 0.7.1 contract for testing"""
    return {
        "id": "test.contract.v1",
        "fluidVersion": "0.7.1",
        "kind": "DataContract",
        "metadata": {"name": "Test Contract", "version": "1.0.0"},
        "schema": [
            {"name": "id", "type": "integer"},
            {"name": "name", "type": "string"},
            {"name": "created_at", "type": "timestamp"},
        ],
        "exposes": [
            {
                "exposeId": "test_table",
                "binding": {"platform": "local", "database": "test_db", "table": "test_table"},
                "contract": {
                    "schema": [
                        {"name": "id", "type": "integer"},
                        {"name": "name", "type": "string"},
                    ]
                },
            }
        ],
    }


@pytest.fixture
def sample_contract_057():
    """Sample FLUID 0.5.7 contract for backward compatibility testing"""
    return {
        "id": "test.contract.v1",
        "fluidVersion": "0.5.7",
        "kind": "DataContract",
        "name": "Test Contract",
        "version": "1.0.0",
        "schema": {
            "fields": [{"name": "id", "type": "integer"}, {"name": "name", "type": "string"}]
        },
        "exposes": [
            {
                "id": "test_table",
                "provider": "local",
                "location": {"database": "test_db", "table": "test_table"},
            }
        ],
    }


@pytest.fixture
def mock_logger():
    """Mock logger for testing"""
    logger = MagicMock()
    logger.info = Mock()
    logger.warning = Mock()
    logger.error = Mock()
    logger.debug = Mock()
    return logger


@pytest.fixture
def mock_console():
    """Mock Rich console for testing"""
    console = MagicMock()
    console.print = Mock()
    console.input = Mock(return_value="yes")
    return console


@pytest.fixture
def cli_context(mock_logger, mock_console):
    """Mock CLI context for testing"""
    context = CLIContext(
        logger=mock_logger, console=mock_console, verbose=False, dry_run=False, yes=False
    )
    return context


@pytest.fixture
def mock_provider():
    """Mock provider for testing"""
    provider = MagicMock()
    provider.name = "mock"
    provider.plan = Mock(return_value=[{"op": "create_table", "table": "test_table"}])
    provider.apply = Mock(return_value=[{"status": "success", "op": "create_table"}])
    return provider


@pytest.fixture
def contract_file(temp_dir, sample_contract):
    """Create a temporary contract YAML file"""
    import yaml

    contract_path = temp_dir / "contract.fluid.yaml"
    with open(contract_path, "w") as f:
        yaml.dump(sample_contract, f)
    return contract_path


@pytest.fixture
def plan_file(temp_dir):
    """Create a temporary plan JSON file"""
    import json

    plan = {
        "metadata": {"generated_at": "2026-01-23T00:00:00Z", "provider": "local"},
        "actions": [{"op": "create_table", "table": "test_table"}],
    }
    plan_path = temp_dir / "plan.json"
    with open(plan_path, "w") as f:
        json.dump(plan, f)
    return plan_path


# Environment configuration for tests
@pytest.fixture(autouse=True)
def test_environment():
    """Set up test environment variables"""
    original_env = os.environ.copy()

    # Set test environment variables
    os.environ["FLUID_ENV"] = "test"
    os.environ["FLUID_LOG_LEVEL"] = "DEBUG"

    yield

    # Restore original environment
    os.environ.clear()
    os.environ.update(original_env)
