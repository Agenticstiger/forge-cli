# FLUID Build CLI Testing Framework

Comprehensive testing framework for validating the FLUID Build CLI and GCP provider functionality with systematic result collection and analysis.

## Overview

This testing framework provides:
- **Comprehensive CLI validation** across all commands and providers
- **Systematic result collection** in timestamped directories
- **Integration test support** with real GCP resources
- **Performance testing** for large-scale scenarios
- **Error condition validation** and edge case handling
- **Detailed reporting** with JSON and human-readable formats

## Quick Start

### Basic Usage

```bash
# Quick validation test (dry-run mode)
./scripts/run_cli_tests.sh --quick --dry-run

# Full test suite with dry-run
./scripts/run_cli_tests.sh --dry-run

# Run with custom output directory
./scripts/run_cli_tests.sh --output ./my-test-results
```

### Integration Testing

For testing with real GCP resources:

```bash
# Set up GCP project for testing
export FLUID_TEST_PROJECT=my-test-project-dev
export FLUID_TEST_REGION=us-central1

# Authenticate (choose one method)
gcloud auth application-default login
# OR
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json

# Run integration tests
./scripts/run_cli_tests.sh --integration
```

## Test Categories

### 1. Basic Commands (`test_basic_commands`)
- CLI help and version information
- Provider listing and discovery
- Doctor diagnostics
- Invalid command handling

### 2. Contract Validation (`test_contract_validation`) 
- Valid FLUID contract parsing
- Schema validation
- Required field checking
- Error reporting for invalid contracts

### 3. Planning Functionality (`test_planning_functionality`)
- **BigQuery contracts**: Datasets, tables, views, partitioning
- **Pub/Sub contracts**: Topics, subscriptions, dead letter queues  
- **Storage contracts**: Buckets, lifecycle policies, IAM
- Action generation and optimization

### 4. Apply Functionality (`test_apply_functionality`)
- Dry-run validation
- Real resource creation (integration mode)
- Idempotent operation testing
- Error recovery and rollback

### 5. Export Functionality (`test_export_functionality`)
- OPDS format export
- DOT graph generation
- Metadata preservation
- Format validation

### 6. Provider-Specific Commands (`test_provider_specific_commands`)
- GCP authentication reporting
- Provider-specific diagnostics
- Service availability checks

### 7. Contract Tests (`test_contract_tests`)
- Schema evolution validation
- Breaking change detection
- Data quality rule enforcement
- Compliance checking

### 8. Error Handling (`test_error_handling`)
- Non-existent file handling
- Malformed YAML processing
- Invalid contract structures
- Network error simulation

### 9. Performance Scenarios (`test_performance_scenarios`)
- Large schema handling (100+ fields)
- Complex contract processing
- Memory and timing benchmarks
- Scalability validation

### 10. CLI Edge Cases (`test_cli_edge_cases`)
- Boundary condition testing
- Output format variations
- Parameter combinations
- Unicode and special characters

## Test Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `FLUID_TEST_PROJECT` | GCP project for integration tests | `fluid-test-project` |
| `FLUID_TEST_REGION` | GCP region for resources | `us-central1` |
| `GOOGLE_APPLICATION_CREDENTIALS` | Service account key path | ADC |
| `FLUID_PROVIDER` | Provider to test | `gcp` |

### Command Line Options

| Option | Description |
|--------|-------------|
| `--dry-run` | Execute tests without real commands |
| `--quick` | Skip performance and long-running tests |
| `--integration` | Enable real GCP resource testing |
| `--provider P` | Test specific provider (default: gcp) |
| `--output DIR` | Custom output directory |
| `--help` | Show usage information |

## Output Structure

Each test run creates a timestamped directory in `runtime/cli_tests_YYYYMMDD_HHMMSS/` containing:

```
runtime/cli_tests_20241014_143022/
├── test_summary.json           # Detailed JSON results
├── test_report.txt             # Human-readable report  
├── test_execution_summary.txt  # Run configuration and status
├── test_data/                  # Generated test contracts
│   ├── valid_contract.fluid.yaml
│   ├── bigquery_contract.fluid.yaml
│   ├── pubsub_contract.fluid.yaml
│   └── ...
├── logs/                       # Command outputs
│   ├── CLI_Help_output.txt
│   ├── Plan_BigQuery_Contract_output.txt
│   └── ...
├── bigquery_plan.json         # Generated plans and exports
├── storage_plan.json
└── export_opds.json
```

### Key Result Files

#### `test_summary.json`
Complete test results in JSON format:
```json
{
  "test_suite": "FLUID Build CLI Comprehensive Tests",
  "timestamp": "2024-10-14T14:30:22",
  "duration_seconds": 45.67,
  "environment": {
    "project": "my-test-project",
    "region": "us-central1",
    "dry_run": false,
    "python_version": "3.9.7"
  },
  "statistics": {
    "total_tests": 25,
    "passed": 23,
    "failed": 2,
    "success_rate": 92.0
  },
  "results": [
    {
      "test_name": "CLI Help",
      "command": "python -m fluid_build.cli --help",
      "success": true,
      "duration_seconds": 0.45,
      "exit_code": 0,
      "timestamp": "2024-10-14T14:30:22"
    }
  ]
}
```

#### `test_report.txt`
Human-readable summary:
```
FLUID Build CLI Test Report
==================================================

Date: 2024-10-14 14:30:22
Duration: 45.67 seconds
Tests: 23/25 passed (92.0%)

Failed Tests:
--------------------
❌ Apply Contract (Real)
   Command: python -m fluid_build.cli apply test.yaml
   Error: Permission denied on project...

All Tests:
--------------------
✅ CLI Help (0.45s)
✅ Version Command (0.32s) 
✅ Plan BigQuery Contract (2.34s)
❌ Apply Contract (Real) (1.23s)
...
```

## Test Contract Examples

The framework generates various test contracts to validate different scenarios:

### BigQuery Contract
```yaml
fluidVersion: "0.5.0"
kind: DataProduct
id: analytics.customer_metrics_v1
name: Customer Metrics
metadata:
  layer: Gold
  owner:
    team: Analytics
    email: analytics@example.com
  policies:
    readers: ["group:analysts@example.com"]
exposes:
  - id: customer_summary
    type: table
    location:
      format: bigquery_table
      properties:
        project: ${project}
        dataset: analytics_gold
        table: customer_summary_v1
    schema:
      - name: customer_id
        type: STRING
        mode: REQUIRED
      - name: total_revenue
        type: NUMERIC
        mode: NULLABLE
    properties:
      partitioning:
        type: TIME
        field: last_purchase_date
      clustering: [customer_id]
build:
  transformation:
    engine: dbt-bigquery
    properties:
      project: ${project}
      dataset: analytics_gold
      target: prod
```

### Pub/Sub Contract
```yaml
fluidVersion: "0.5.0"
kind: DataProduct
id: events.user_actions_v1
name: User Actions Events
exposes:
  - id: user_events_topic
    type: pubsub_topic
    location:
      format: pubsub_topic
      properties:
        project: ${project}
        topic: user-events-v1
  - id: user_events_subscription
    type: pubsub_subscription
    location:
      format: pubsub_subscription
      properties:
        project: ${project}
        subscription: user-events-analytics-v1
        topic: user-events-v1
```

## Running Specific Test Categories

You can modify the test script to run specific categories:

```python
# In test_cli_comprehensive.py, comment out unwanted tests:
def run_all_tests(self):
    # self.test_basic_commands()          # Basic CLI
    self.test_planning_functionality()    # Only planning tests
    # self.test_apply_functionality()     # Skip apply tests
    # self.test_performance_scenarios()   # Skip performance
```

## Integration Test Setup

### GCP Project Preparation

1. **Create test project**:
   ```bash
   gcloud projects create fluid-test-project-$(date +%s)
   export FLUID_TEST_PROJECT=fluid-test-project-xyz
   ```

2. **Enable required APIs**:
   ```bash
   gcloud services enable bigquery.googleapis.com \
     storage.googleapis.com \
     pubsub.googleapis.com \
     composer.googleapis.com \
     --project=$FLUID_TEST_PROJECT
   ```

3. **Set up authentication**:
   ```bash
   # Option 1: User credentials
   gcloud auth application-default login
   
   # Option 2: Service account
   gcloud iam service-accounts create fluid-test \
     --project=$FLUID_TEST_PROJECT
   
   gcloud projects add-iam-policy-binding $FLUID_TEST_PROJECT \
     --member="serviceAccount:fluid-test@$FLUID_TEST_PROJECT.iam.gserviceaccount.com" \
     --role="roles/editor"
   
   gcloud iam service-accounts keys create ./fluid-test-sa.json \
     --iam-account=fluid-test@$FLUID_TEST_PROJECT.iam.gserviceaccount.com
   
   export GOOGLE_APPLICATION_CREDENTIALS=./fluid-test-sa.json
   ```

### Resource Cleanup

Integration tests create resources that may incur costs. Clean up with:

```bash
# List resources created by tests
bq ls --project_id=$FLUID_TEST_PROJECT
gsutil ls -p $FLUID_TEST_PROJECT
gcloud pubsub topics list --project=$FLUID_TEST_PROJECT

# Clean up datasets
bq rm -r -f -d $FLUID_TEST_PROJECT:test_dataset
bq rm -r -f -d $FLUID_TEST_PROJECT:analytics_gold

# Clean up buckets (be careful!)
gsutil rm -r gs://$FLUID_TEST_PROJECT-test-bucket

# Clean up pub/sub
gcloud pubsub topics delete user-events-v1 --project=$FLUID_TEST_PROJECT
```

## Troubleshooting

### Common Issues

1. **Import Errors**:
   ```bash
   # Ensure project root is in Python path
   export PYTHONPATH="/path/to/fluid-forge-super-ultimate:$PYTHONPATH"
   ```

2. **Permission Denied**:
   ```bash
   # Check GCP authentication
   gcloud auth list
   gcloud config get-value project
   ```

3. **Test Failures**:
   ```bash
   # Check detailed logs
   cat runtime/cli_tests_*/logs/Test_Name_output.txt
   ```

4. **Timeout Issues**:
   ```bash
   # Increase timeout in test script
   # Edit timeout parameter in subprocess.run()
   ```

### Debug Mode

Enable verbose logging:

```python
# In test_cli_comprehensive.py
logging.basicConfig(level=logging.DEBUG)
```

## Extending the Test Suite

### Adding New Tests

1. **Create test method**:
   ```python
   def test_new_functionality(self):
       logger.info("=== Testing New Functionality ===")
       
       # Create test contract
       contract = {...}
       contract_file = self.create_test_contract("new_test", contract)
       
       # Run command
       self.run_command([
           "python", "-m", "fluid_build.cli", "new-command",
           str(contract_file)
       ], "New Functionality Test")
   ```

2. **Add to test suite**:
   ```python
   def run_all_tests(self):
       # ... existing tests ...
       self.test_new_functionality()
   ```

### Custom Test Contracts

Create domain-specific test contracts:

```python
def create_ml_pipeline_contract(self):
    return {
        "fluidVersion": "0.5.0",
        "kind": "DataProduct",
        "id": "ml.feature_pipeline_v1",
        # ... ML-specific configuration
    }
```

## Performance Benchmarks

The test suite includes performance benchmarks:

- **Large schema processing**: 100+ field tables
- **Complex contract parsing**: Nested structures
- **Memory usage tracking**: Resource consumption
- **Execution timing**: Command performance

Benchmark results are included in test summaries for regression detection.

## CI/CD Integration

### GitHub Actions Example

```yaml
name: CLI Tests
on: [push, pull_request]

jobs:
  cli-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      
      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.9'
      
      - name: Install dependencies
        run: pip install -r requirements.txt
      
      - name: Run CLI tests (dry-run)
        run: ./scripts/run_cli_tests.sh --quick --dry-run
      
      - name: Upload test results
        uses: actions/upload-artifact@v3
        with:
          name: cli-test-results
          path: runtime/cli_tests_*/
```

### Integration Test Pipeline

```yaml
integration-tests:
  runs-on: ubuntu-latest
  if: github.ref == 'refs/heads/main'
  steps:
    - name: Authenticate to GCP
      uses: google-github-actions/auth@v1
      with:
        credentials_json: ${{ secrets.GCP_SA_KEY }}
    
    - name: Run integration tests
      env:
        FLUID_TEST_PROJECT: ${{ secrets.GCP_TEST_PROJECT }}
      run: ./scripts/run_cli_tests.sh --integration --quick
```

## Support

For issues with the test framework:

1. Check the generated logs in `runtime/cli_tests_*/logs/`
2. Review the test summary for specific failures
3. Validate environment setup and dependencies
4. Run individual test categories to isolate issues

The comprehensive test suite ensures the FLUID Build CLI and GCP provider work reliably across all supported scenarios and configurations.