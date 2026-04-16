# FLUID Init Command - Test Suite Documentation

## Overview

Comprehensive test suite for the `fluid init` command covering:
- Command registration and routing
- CI/CD pipeline generation (Jenkins, GitHub Actions, GitLab CI)
- Agent Zero scanner (dbt/Terraform/SQL detection)
- PII detection with confidence scoring
- FLUID contract generation
- Governance policy automation
- End-to-end integration workflows

## Test Structure

```
tests/cli/
├── test_init.py                  # Unit tests (600+ lines)
└── test_init_integration.py      # Integration tests (500+ lines)
```

## Test Coverage

### Unit Tests (`test_init.py`)

#### 1. TestInitCommand
- ✅ Command registration
- ✅ Parser creation
- ✅ Handler function existence

#### 2. TestCICDGeneration
- ✅ Jenkinsfile generation and validation
- ✅ All 6 pipeline stages present
- ✅ FLUID 0.7.1 version locked
- ✅ GitHub Actions workflow generation
- ✅ YAML structure validation
- ✅ GitLab CI configuration generation
- ✅ Multi-platform support

#### 3. TestAgentZeroScanner
- ✅ dbt project detection
- ✅ Project scanning
- ✅ SQL model discovery
- ✅ Column extraction from SQL
- ✅ dbt_project.yml parsing
- ✅ profiles.yml target detection

#### 4. TestPIIDetection
- ✅ SSN detection (90% confidence)
- ✅ Email detection (85% confidence)
- ✅ Credit card detection (95% confidence)
- ✅ Phone number detection (80% confidence)
- ✅ Date of birth detection (85% confidence)
- ✅ Address detection (70% confidence)
- ✅ Name detection (60% confidence)
- ✅ Confidence score validation
- ✅ Clean data returns empty

#### 5. TestContractGeneration
- ✅ Valid FLUID 0.7.1 contract structure
- ✅ All models included as 'produces'
- ✅ Column schemas preserved
- ✅ Type mapping accuracy

#### 6. TestGovernancePolicies
- ✅ Masking rule generation
- ✅ High confidence → SHA256
- ✅ Medium confidence → MASK
- ✅ All PII covered
- ✅ Policy structure validation

#### 7. TestProjectDetection
- ✅ dbt project detection (dbt_project.yml)
- ✅ Terraform detection (*.tf files)
- ✅ SQL files detection (*.sql)
- ✅ Empty directory handling

### Integration Tests (`test_init_integration.py`)

#### 1. TestQuickstartIntegration
- ✅ Complete project creation
- ✅ FLUID 0.7.1 contract generation
- ✅ Sample data inclusion
- ✅ Directory structure validation

#### 2. TestScanModeIntegration
- ✅ Complex dbt project with 3 models
- ✅ Multiple PII types detection
- ✅ All 7 PII types coverage
- ✅ Valid FLUID contract generation
- ✅ Governance policy application
- ✅ Critical PII masking

#### 3. TestCICDIntegration
- ✅ Complete workflow execution
- ✅ All 3 platforms (Jenkins, GitHub, GitLab)
- ✅ Valid YAML generation
- ✅ FLUID 0.7.1 version locking

#### 4. TestEndToEndWorkflow
- ✅ Complete dbt migration:
  - Scan → Contract → Governance → CI/CD
- ✅ User input simulation
- ✅ Multi-step workflow validation
- ✅ Artifact verification

## Running Tests

### Run All Tests
```bash
cd /home/dustlabs/fluid-mono/fluid_forge/fluid-forge-cli

# Run all init tests
pytest tests/cli/test_init.py -v
pytest tests/cli/test_init_integration.py -v

# Run specific test class
pytest tests/cli/test_init.py::TestPIIDetection -v

# Run specific test
pytest tests/cli/test_init.py::TestPIIDetection::test_detect_pii_finds_ssn -v
```

### Run with Coverage
```bash
# Install coverage
pip install pytest-cov

# Run with coverage report
pytest tests/cli/test_init.py tests/cli/test_init_integration.py \
  --cov=fluid_build.cli.init \
  --cov-report=html \
  --cov-report=term

# View HTML report
open htmlcov/index.html
```

### Run Quick Smoke Tests
```bash
# Just the fast unit tests
pytest tests/cli/test_init.py -k "not Integration" -v

# Just CI/CD tests
pytest tests/cli/test_init.py::TestCICDGeneration -v
```

## Test Data

### Mock dbt Project
```yaml
# dbt_project.yml
name: customer_analytics
version: 1.0.0
profile: analytics
model-paths: [models]

# profiles.yml
analytics:
  target: dev
  outputs:
    dev:
      type: bigquery
      project: my-project
      dataset: analytics
      location: US
```

### Sample SQL Models

**dim_customers.sql** (12 columns, 7 PII fields):
```sql
SELECT
    customer_id,
    email,              -- PII: EMAIL (85%)
    first_name,         -- PII: NAME (60%)
    last_name,          -- PII: NAME (60%)
    ssn,                -- PII: SSN (90%)
    date_of_birth,      -- PII: DATE_OF_BIRTH (85%)
    phone_number,       -- PII: PHONE (80%)
    shipping_address,   -- PII: ADDRESS (70%)
    billing_address,    -- PII: ADDRESS (70%)
    credit_card_number, -- PII: CREDIT_CARD (95%)
    created_at,
    updated_at
FROM raw_customers
```

**fact_orders.sql** (6 columns, no PII):
```sql
SELECT
    order_id,
    customer_id,
    order_date,
    total_amount,
    status,
    created_at
FROM raw_orders
```

**customer_ltv.sql** (5 columns, 1 PII field):
```sql
SELECT
    c.customer_id,
    c.email,            -- PII: EMAIL (85%)
    COUNT(o.order_id) as order_count,
    SUM(o.total_amount) as lifetime_value,
    CASE WHEN SUM(o.total_amount) > 10000 THEN 'VIP' END as tier
FROM dim_customers c
LEFT JOIN fact_orders o ON c.customer_id = o.customer_id
GROUP BY c.customer_id, c.email
```

## Expected Test Results

### PII Detection Accuracy
Based on test data, the detector should find:

| Model | PII Columns | Total Columns | Detection Rate |
|-------|-------------|---------------|----------------|
| dim_customers | 7 | 12 | 100% |
| fact_orders | 0 | 6 | N/A |
| customer_ltv | 1 | 5 | 100% |

### Governance Policies Applied
Expected masking rules:

| Column | PII Type | Confidence | Method |
|--------|----------|------------|--------|
| ssn | SSN | 90% | SHA256 |
| credit_card_number | CREDIT_CARD | 95% | SHA256 |
| email | EMAIL | 85% | SHA256 |
| phone_number | PHONE | 80% | MASK |
| date_of_birth | DATE_OF_BIRTH | 85% | SHA256 |
| shipping_address | ADDRESS | 70% | MASK |
| billing_address | ADDRESS | 70% | MASK |
| first_name | NAME | 60% | MASK |
| last_name | NAME | 60% | MASK |

### CI/CD Validation
All generated pipelines must:
- ✅ Use `FLUID_VERSION="0.7.1"`
- ✅ Include Validate stage
- ✅ Include Plan stage
- ✅ Include Apply stage (main branch only)
- ✅ Include Test stage
- ✅ Be valid YAML/Groovy syntax

## Debugging Failed Tests

### Common Issues

#### 1. Import Errors
```bash
# Error: ModuleNotFoundError: No module named 'fluid_build'
# Fix: Ensure package is installed
pip install -e .
```

#### 2. Path Issues
```bash
# Error: FileNotFoundError
# Fix: Check test uses correct temp directory
# Verify: Mock Path.cwd() is set properly
```

#### 3. Mock Issues
```bash
# Error: AttributeError: Mock object has no attribute 'X'
# Fix: Configure mock properly
mock_logger.info = Mock()
mock_logger.error = Mock()
```

### Test Debugging Commands

```bash
# Run with verbose output
pytest tests/cli/test_init.py::TestPIIDetection::test_detect_pii_finds_ssn -vv

# Show print statements
pytest tests/cli/test_init.py -s

# Drop into debugger on failure
pytest tests/cli/test_init.py --pdb

# Show locals on failure
pytest tests/cli/test_init.py -l
```

## Test Maintenance

### Adding New Tests

1. **Add unit test**:
```python
# tests/cli/test_init.py

def test_new_feature(self):
    """Test description."""
    # Arrange
    test_data = {...}
    
    # Act
    result = init.new_function(test_data)
    
    # Assert
    self.assertEqual(result, expected)
```

2. **Add integration test**:
```python
# tests/cli/test_init_integration.py

def test_new_workflow(self):
    """Test complete workflow."""
    # Setup fixtures
    # Execute workflow
    # Verify end state
```

3. **Run new tests**:
```bash
pytest tests/cli/test_init.py::TestClass::test_new_feature -v
```

### Updating Test Data

When adding new PII types or updating detection logic:

1. Update mock SQL in `setUp()` methods
2. Update expected results in assertions
3. Add new test cases for edge cases
4. Update this documentation

## Performance Benchmarks

Expected test execution times (on standard dev machine):

| Test Suite | Tests | Time | Speed |
|------------|-------|------|-------|
| Unit Tests | 40+ | ~5 sec | Fast |
| Integration Tests | 15+ | ~10 sec | Medium |
| Full Suite | 55+ | ~15 sec | Good |

**Note**: Tests use `tempfile` and mocks for speed. No actual cloud resources needed.

## Test Success Criteria

All tests must pass with:
- ✅ 100% pass rate
- ✅ No warnings
- ✅ No deprecation notices
- ✅ Coverage > 85%
- ✅ Execution time < 30 seconds

## CI/CD Integration

### GitHub Actions
```yaml
name: Test FLUID Init

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v2
      - uses: actions/setup-python@v2
        with:
          python-version: '3.9'
      - run: pip install -e .
      - run: pip install pytest pytest-cov
      - run: pytest tests/cli/test_init.py tests/cli/test_init_integration.py -v
```

### Jenkins
```groovy
stage('Test Init Command') {
    steps {
        sh 'pytest tests/cli/test_init*.py -v --junitxml=results.xml'
    }
    post {
        always {
            junit 'results.xml'
        }
    }
}
```

## Related Documentation

- [FLUID_INIT_COMPLETE_STATUS.md](../../../FLUID_INIT_COMPLETE_STATUS.md) - Implementation status
- [FLUID_AGENT_ZERO_SCANNER.md](../../../FLUID_AGENT_ZERO_SCANNER.md) - Scanner deep dive
- [FLUID_INIT_CICD_INTEGRATION.md](../../../FLUID_INIT_CICD_INTEGRATION.md) - CI/CD details
- [fluid_build/cli/init.py](../../../fluid_build/cli/init.py) - Source code

## Support

For test failures or questions:
1. Check this documentation
2. Review test output carefully
3. Use `pytest -vv` for verbose output
4. Check mock configuration
5. Verify test fixtures are set up correctly
