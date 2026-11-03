from pathlib import Path

import pytest
import yaml


@pytest.fixture
def contract():
    root = Path(__file__).resolve().parents[2]
    return yaml.safe_load((root / "examples/flink-iceberg/contract.fluid.yaml").read_text())
