# FLUID Build - Comprehensive Testing Framework

This directory contains a unified testing orchestrator that consolidates all testing capabilities into a single, powerful command-line interface.

## 🚀 Quick Start

### Single Command Testing

```bash
# Quick validation (recommended first run)
./scripts/run_all_tests.sh --quick --dry-run

# Full test suite
./scripts/run_all_tests.sh

# Integration tests with cloud provider
./scripts/run_all_tests.sh --provider gcp --project my-test-project
```

## 📋 What Gets Tested

The comprehensive test orchestrator runs:

| Category | Description | Tests |
|----------|-------------|-------|
| **CLI Comprehensive** | Complete CLI functionality validation | All commands, providers, validation, planning, apply |
| **AWS Provider** | AWS provider implementation testing | Service integration, authentication, resource management |
| **System Diagnostics** | Health checks and configuration | Environment, dependencies, provider discovery |
| **Contract Validation** | FLUID contract parsing and validation | Schema validation, example contracts, error handling |
| **Performance Benchmarks** | Load testing and performance analysis | Large contracts, memory usage, execution timing |
| **Error Conditions** | Error handling and edge cases | Invalid inputs, malformed data, failure scenarios |

## 📁 Output Structure

Each test run creates a timestamped directory in `runtime/test_runs/`:

```
runtime/test_runs/20241014_143022/
├── reports/
│   ├── comprehensive_report.html    # 📊 Interactive HTML dashboard
│   ├── comprehensive_summary.json   # 📋 Detailed JSON results
│   └── summary.txt                  # 📝 Text summary
├── logs/                            # 📁 Detailed command outputs
│   ├── CLI_Basic_Commands.log
│   ├── AWS_Provider_Tests.log
│   └── ...
├── artifacts/                       # 📁 Generated plans and exports
│   ├── plans/
│   ├── exports/
│   └── ...
├── data/                           # 📁 Test contracts and data files
│   ├── test_contracts/
│   └── generated_data/
├── diagnostics/                    # 📁 System diagnostic results
│   ├── env.txt
│   ├── providers.json
│   └── ...
└── coverage/                       # 📁 Performance and coverage data
```

## 🛠️ Command Options

### Basic Options

```bash
--quick                 # Skip long-running performance tests
--dry-run              # Run tests without executing actual commands
--provider PROVIDER    # Test specific provider (local|gcp|aws|snowflake)
--project PROJECT      # Cloud project/account ID for integration tests
--region REGION        # Cloud region (default: us-central1)
--output-dir DIR       # Custom output directory
--log-level LEVEL      # Logging verbosity (DEBUG|INFO|WARNING|ERROR)
```

### Test Suite Control

```bash
--skip-cli             # Skip CLI comprehensive tests
--skip-aws             # Skip AWS provider tests
--skip-diagnostics     # Skip system diagnostics
--skip-validation      # Skip contract validation tests
--skip-performance     # Skip performance benchmarks
--skip-errors          # Skip error condition tests
```

### Output Control

```bash
--no-bundle            # Don't create compressed result bundle
--help                 # Show detailed help message
```

## 🎯 Usage Examples

### Development Testing

```bash
# Quick validation during development
./scripts/run_all_tests.sh --quick --dry-run

# Focus on specific components
./scripts/run_all_tests.sh --skip-performance --skip-diagnostics

# Debug mode with verbose output
./scripts/run_all_tests.sh --log-level DEBUG --quick
```

### Integration Testing

```bash
# Local provider (safe, no cloud resources)
./scripts/run_all_tests.sh --provider local

# GCP integration test
export FLUID_TEST_PROJECT=my-gcp-project
./scripts/run_all_tests.sh --provider gcp --project my-gcp-project

# AWS provider test
./scripts/run_all_tests.sh --provider aws --skip-performance
```

### CI/CD Pipeline

```bash
# CI-friendly test run
./scripts/run_all_tests.sh --quick --dry-run --no-bundle

# Full validation for releases
./scripts/run_all_tests.sh --provider local
```

## 📊 Results Analysis

### HTML Report

The interactive HTML report (`reports/comprehensive_report.html`) provides:

- **Overall Statistics**: Pass/fail rates, duration, success metrics
- **Test Suite Breakdown**: Detailed results by category
- **Configuration Summary**: Environment and settings used
- **Failure Analysis**: Detailed error information for failed tests
- **Artifact Links**: Direct access to logs and generated files

### JSON Summary

The JSON summary (`reports/comprehensive_summary.json`) contains:

```json
{
  "orchestrator": {
    "name": "FLUID Build Comprehensive Test Orchestrator",
    "timestamp": "2024-10-14T14:30:22",
    "duration_seconds": 245.67,
    "configuration": {...}
  },
  "overall_statistics": {
    "total_tests": 45,
    "passed": 42,
    "failed": 3,
    "success_rate": 93.3
  },
  "test_suites": [...]
}
```

### Text Summary

Quick overview in `reports/summary.txt`:

```
FLUID Build - Comprehensive Test Report
======================================

Generated: 2024-10-14T14:30:22
Duration: 245.67 seconds

OVERALL RESULTS
---------------
Total Tests: 45
Passed: 42
Failed: 3
Success Rate: 93.3%

FAILED TESTS
------------
❌ Apply Contract (Real) (CLI)
   Command: python -m fluid_build.cli apply test.yaml
   Exit Code: 1

TEST SUITE SUMMARIES
-------------------
📋 CLI Comprehensive Tests
   Tests: 18/20 passed (90.0%)
   Duration: 120.34s
...
```

## 🔧 Environment Setup

### Prerequisites

- **Python 3.9+** with required packages
- **Virtual environment** (recommended)
- **Cloud CLI tools** (optional, for integration tests)

### Quick Setup

```bash
# Recommended: Set up environment first
./scripts/setup_environment.sh

# Or install dependencies manually
pip3 install PyYAML jsonschema rich
pip3 install -e .
```

### Environment Variables

```bash
# Test configuration
export FLUID_TEST_PROJECT=my-test-project
export FLUID_TEST_REGION=us-central1

# GCP authentication
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
# OR use: gcloud auth application-default login

# AWS authentication  
export AWS_ACCESS_KEY_ID=your-access-key
export AWS_SECRET_ACCESS_KEY=your-secret-key
# OR use: aws configure
```

### Virtual Environment

```bash
# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate  # Linux/Mac
# .venv\Scripts\Activate.ps1  # Windows

# Install dependencies
pip install -r requirements.txt
pip install -e .
```

## 🚨 Troubleshooting

### Common Issues

1. **Permission Denied**
   ```bash
   chmod +x scripts/run_all_tests.sh
   ```

2. **Python Import Errors** (PyYAML missing)
   ```bash
   # Quick fix
   pip3 install PyYAML
   
   # Complete setup
   ./scripts/setup_environment.sh
   ```

3. **CLI Module Not Found**
   ```bash
   export PYTHONPATH="$(pwd):$PYTHONPATH"
   pip install -e .
   ```

4. **Contract Validation Failures**
   - Ensure PyYAML is installed: `python3 -c "import yaml"`
   - Check contract syntax with: `python3 -m fluid_build.cli validate path/to/contract.yaml`

5. **Cloud Authentication**
   ```bash
   # GCP
   gcloud auth application-default login
   gcloud config set project YOUR_PROJECT
   
   # AWS
   aws configure
   aws sts get-caller-identity
   ```

4. **Test Failures**
   - Check the HTML report for detailed failure analysis
   - Review specific log files in `logs/` directory
   - Run with `--log-level DEBUG` for verbose output
   - Use `--skip-*` flags to isolate failing categories

### Debug Mode

```bash
# Maximum verbosity
./scripts/run_all_tests.sh --log-level DEBUG --quick

# Isolate specific test category
./scripts/run_all_tests.sh --skip-cli --skip-diagnostics --skip-performance
```

## 📈 Performance Considerations

### Test Duration

| Mode | Duration | Description |
|------|----------|-------------|
| `--quick --dry-run` | ~30 seconds | Fastest validation |
| `--quick` | ~2 minutes | Skip performance tests |
| Default | ~5-10 minutes | Full test suite |
| Integration | ~10-20 minutes | With real cloud resources |

### Resource Usage

- **CPU**: Moderate usage during test execution
- **Memory**: ~500MB peak for large contract testing
- **Disk**: ~50-100MB per test run (logs and artifacts)
- **Network**: Minimal (only for cloud integration tests)

## 🔄 Integration with Existing Scripts

The orchestrator consolidates these existing scripts:

- `test_cli_comprehensive.py` → CLI testing module
- `diagnose.sh` → System diagnostics module  
- `run_cli_tests.sh` → Integrated into orchestrator
- `ccli_validate.sh` → Contract validation module
- `cli_audit.sh` → System audit module

## 📦 Result Bundling

Results are automatically bundled for easy sharing:

```bash
# Bundle created automatically
runtime/test_runs/fluid_test_results_20241014_143022.tar.gz

# Disable bundling
./scripts/run_all_tests.sh --no-bundle
```

## 🤝 Contributing

To add new test categories:

1. Add test method to `ComprehensiveTestOrchestrator` class
2. Update configuration options and CLI arguments
3. Add documentation to this README
4. Test with `--dry-run` mode first

## 📞 Support

For issues or questions:

1. Run with `--log-level DEBUG` for detailed output
2. Check the comprehensive HTML report
3. Include the complete test results bundle when reporting issues
4. Review the `logs/` directory for command-specific failures

---

**🎉 The comprehensive test orchestrator ensures FLUID Build quality across all components with a single command!**