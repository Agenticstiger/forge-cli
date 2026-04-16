# FLUID Build Consolidated Test Suite

This is the consolidated test structure for FLUID Build, designed to provide comprehensive coverage of CLI commands, runtimes, error handling, and integration scenarios.

## Test Organization

```
tests/
├── README.md                    # This file
├── TESTING_GUIDE.md            # Comprehensive testing guide
├── test_runner.py              # Enhanced test runner with coverage
├── test_error_framework.py     # Error handling tests
├── test_policy_engine.py       # Policy engine tests
├── test_policy_check_cli.py    # Policy CLI tests
├── run_tests.py                # Legacy test orchestrator
├── cli/                        # CLI command tests (24 files)
│   ├── base_test.py           # Base test utilities
│   ├── test_validate.py       # Contract validation
│   ├── test_plan.py           # Plan generation
│   ├── test_apply.py          # Resource deployment
│   ├── test_diff.py           # Drift detection ⭐ NEW
│   ├── test_product_add.py    # Add sources/exposures ⭐ NEW
│   ├── test_forge.py          # Code generation
│   ├── test_doctor.py         # System diagnostics
│   └── ... (20+ more)
├── runtimes/                   # Runtime integration tests
│   └── test_dataform_gcp.py   # Dataform runtime ⭐ NEW
└── individual/                 # Individual test modules
    └── ... (legacy tests)
```

## Quick Start

### Run All Tests

```bash
# Standard test run
python tests/test_runner.py

# With coverage report
python tests/test_runner.py --coverage

# Via admin command
fluid admin test
```

### Run Specific Tests

```bash
# CLI tests only
python tests/test_runner.py --cli

# Runtime tests only
python tests/test_runner.py --runtime

# Specific test files
python tests/test_runner.py diff product_add

# Single test file directly
python tests/cli/test_diff.py
```

### Optional Live Snowflake Smoke Test

```bash
# Required env vars:
#   SNOWFLAKE_ACCOUNT
#   SNOWFLAKE_USER
#   SNOWFLAKE_PASSWORD
#   SNOWFLAKE_WAREHOUSE
#   FLUID_TEST_SNOWFLAKE_DATABASE
# Optional:
#   SNOWFLAKE_ROLE

.venv/bin/pytest tests/providers/test_snowflake_governance_live.py -q
```

Use a disposable Snowflake database for `FLUID_TEST_SNOWFLAKE_DATABASE`. The smoke
test creates and drops a temporary schema inside that database.

## New Test Coverage (Recent Additions)

### ✅ diff Command Tests (`tests/cli/test_diff.py`)
- Command registration and argument parsing
- Drift detection (added/removed/unchanged resources)
- State file comparison
- Exit codes with `--exit-on-drift` flag
- Report format validation
- Resource ID extraction logic

**Key Tests:**
- `test_diff_with_no_state_shows_all_added()` - No previous state
- `test_diff_detects_removed_resources()` - Resource removal detection
- `test_diff_exit_code_with_drift()` - CI/CD integration
- `test_diff_output_format()` - Report structure validation

### ✅ product-add Command Tests (`tests/cli/test_product_add.py`)
- Adding sources with all options
- Adding exposures with descriptions
- Adding data quality checks
- Deduplication logic
- Contract modification
- JSON output validation

**Key Tests:**
- `test_add_source_to_contract()` - Source addition
- `test_add_exposure_to_contract()` - Exposure addition
- `test_deduplication_on_add()` - Duplicate handling
- `test_get_section_key()` - Section mapping

### ✅ Dataform Runtime Tests (`tests/runtimes/test_dataform_gcp.py`)
- Google Cloud Dataform API integration
- Compilation with git references
- Polling for completion
- Error handling
- Stub implementation
- Timeout handling

**Key Tests:**
- `test_stub_implementation_without_library()` - Graceful degradation
- `test_dataform_with_mock_api()` - API mocking
- `test_dataform_compilation_errors()` - Error reporting
- `test_wait_for_compilation()` - Polling logic

### ✅ Error Framework Tests (`tests/test_error_framework.py`)
- Error codes (E001-E999)
- Error categories and severity
- Auto-fix capabilities
- CI/CD formatting
- Helper functions
- Exception conversion

**Key Tests:**
- `test_fluid_error_with_auto_fix()` - Auto-fix execution
- `test_format_error_for_ci_github()` - GitHub Actions format
- `test_handle_exception_converts_file_not_found()` - Exception wrapping
- `test_create_provider_error_gcp()` - Provider-specific errors

## Test Coverage Summary

### Coverage by Component

| Component | Files | Tests | Coverage |
|-----------|-------|-------|----------|
| CLI Commands | 24 | 450+ | 95%+ |
| Runtimes | 1 | 12 | 85%+ |
| Error Framework | 1 | 40+ | 100% |
| Policy Engine | 2 | 20+ | 90%+ |
| **Total** | **28+** | **520+** | **93%+** |

## Running Tests with Coverage

```bash
# Generate coverage report
python tests/test_runner.py --coverage

# View HTML report
# Windows:
start runtime\coverage\index.html

# macOS/Linux:
open runtime/coverage/index.html
```

## Test Development

### Using Base Test Class

All CLI tests should extend `CLITestCase`:

```python
from tests.cli.base_test import CLITestCase

class TestMyCommand(CLITestCase):
    def test_command_works(self):
        contract_path = self.create_test_contract()
        args = self.mock_args(contract=str(contract_path))
        logger = self.mock_logger()
        
        result = my_command.run(args, logger)
        self.assertEqual(result, 0)
```

### Available Utilities

- `create_test_contract()` - Generate test contracts
- `create_test_plan()` - Generate test plans
- `mock_logger()` - Mock logger instance
- `mock_args()` - Mock command arguments
- `assert_json_output()` - Validate JSON
- `assert_file_exists()` - Check files
- `assert_file_contains()` - Check content

## Continuous Integration

### GitHub Actions

```yaml
- name: Run tests with coverage
  run: python tests/test_runner.py --coverage
  
- name: Upload coverage
  uses: codecov/codecov-action@v3
```

### Pre-commit Hook

```bash
#!/bin/bash
python tests/test_runner.py --cli --verbose 1
```

## Test Categories

### 🖥️ CLI Tests (`tests/cli/`)
Core CLI commands, validation, planning, drift detection

### ⚙️ Runtime Tests (`tests/runtimes/`)
dbt, Dataform, and other runtime integrations

### 🔒 Policy Tests (`tests/test_policy_*.py`)
Policy engine, compliance, guardrails

### 🐛 Error Tests (`tests/test_error_framework.py`)
Error handling, codes, formatting, auto-fix

### 🔄 Integration Tests
End-to-end workflows, multi-component tests

## Documentation

See [TESTING_GUIDE.md](./TESTING_GUIDE.md) for:
- Detailed testing guide
- Writing new tests
- Best practices
- Troubleshooting
- Performance testing
- Contributing guidelines

## Recent Improvements

✅ **December 2025:**
- Added comprehensive tests for new `diff` command
- Added tests for `product-add` command with deduplication
- Added Dataform runtime integration tests
- Enhanced error framework tests (40+ test cases)
- Created comprehensive testing guide
- Improved test runner with coverage support

## Next Steps

- [ ] Add integration tests for multi-command workflows
- [ ] Add performance benchmarks for large contracts
- [ ] Add load tests for concurrent operations
- [ ] Expand provider-specific tests
- [ ] Add E2E tests with real cloud resources (optional)

## Support

For issues or questions about testing:
1. Check [TESTING_GUIDE.md](./TESTING_GUIDE.md)
2. Review existing test files for patterns
3. Check test output for specific errors

### Report Locations
Reports are saved to `runtime/test_runs/run_TIMESTAMP/`:
- `consolidated_test_report.html` - Interactive HTML report
- `consolidated_test_report.json` - Machine-readable results

## Design Philosophy

This consolidated test structure emphasizes:

1. **Command Visibility**: Every test clearly shows the exact command being executed
2. **Clean Structure**: Organized by logical test categories
3. **Shared Utilities**: Common framework eliminates code duplication
4. **Rich Reporting**: HTML reports focus on usability over complex summaries
5. **Easy Integration**: Works with existing `fluid admin test` command
6. **Individual Execution**: Each test file can run independently

## Migration from Legacy Tests

The new structure replaces these legacy test scripts:
- `scripts/run_all_tests.py` ❌
- `scripts/test_all.py` ❌  
- `scripts/ultimate_test_suite.py` ❌

All functionality has been consolidated into the new structure with improved:
- Organization and maintainability
- Report quality and command visibility
- Execution consistency and reliability

## Integration with Admin Command

The `fluid admin test` command automatically detects and uses the new consolidated test structure when available, falling back to legacy scripts for backward compatibility.

### Quick Mode Mapping
- `fluid admin test --quick` runs: CLI + Admin test categories
- `fluid admin test` (full) runs: All test categories

## Adding New Tests

To add new tests:

1. **Choose the appropriate category** (cli, core, templates, providers, integration)
2. **Add test definitions** to the corresponding `get_*_tests()` function
3. **Follow the test structure**: 
   ```python
   {
       'name': 'Test Name',
       'description': 'What this test validates',
       'command': ['command', 'args'],
       'expect_success': True  # or False for negative tests
   }
   ```
4. **Test individually** before committing: `python tests/category/test_file.py`

The shared framework handles all execution, reporting, and integration automatically.
