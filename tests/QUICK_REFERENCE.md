# FLUID Build - Quick Test Reference

## 🚀 Quick Commands

### Run All Tests
```bash
python tests/test_runner.py
```

### Run with Coverage
```bash
python tests/test_runner.py --coverage
# Report: runtime/coverage/index.html
```

### Run Specific Categories
```bash
python tests/test_runner.py --cli        # CLI commands only
python tests/test_runner.py --runtime    # Runtimes only
```

### Run Specific Tests
```bash
python tests/test_runner.py diff product_add
```

### Individual Test Files
```bash
python tests/cli/test_diff.py
python tests/cli/test_product_add.py
python tests/runtimes/test_dataform_gcp.py
python tests/test_error_framework.py
```

## 📊 Current Coverage

| Component | Coverage |
|-----------|----------|
| CLI Commands | 95%+ |
| Runtimes | 85%+ |
| Error Framework | 100% |
| Overall | 93%+ |

## 🆕 Recently Added Tests

- ✅ `test_diff.py` - Drift detection (15 tests)
- ✅ `test_product_add.py` - Add sources/exposures (18 tests)
- ✅ `test_dataform_gcp.py` - Dataform runtime (12 tests)
- ✅ `test_error_framework.py` - Enhanced errors (40+ tests)

**Total: 105+ new tests | 1,500+ lines of test code**

## 🧪 Test Structure

```
tests/
├── cli/                    # 24 test files, 450+ tests
├── runtimes/              # 1 test file, 12 tests
├── test_error_framework.py # 40+ tests
├── test_policy_engine.py   # 20+ tests
└── test_runner.py         # Enhanced runner
```

## 📝 Test Development

### Using Base Class
```python
from tests.cli.base_test import CLITestCase

class TestMyCommand(CLITestCase):
    def test_command(self):
        args = self.mock_args(contract='test.yaml')
        logger = self.mock_logger()
        result = my_command.run(args, logger)
        self.assertEqual(result, 0)
```

### Utilities Available
- `create_test_contract()` - Generate contracts
- `create_test_plan()` - Generate plans
- `mock_logger()` - Mock logger
- `mock_args()` - Mock arguments
- `assert_json_output()` - Validate JSON
- `assert_file_exists()` - Check files

## 🔍 Debugging

### Verbose Output
```bash
python tests/test_runner.py --verbose 2
```

### Single Test Debug
```bash
python -m pdb tests/cli/test_diff.py
```

## 📚 Documentation

- **Comprehensive Guide:** [TESTING_GUIDE.md](./TESTING_GUIDE.md)
- **Test Structure:** [README.md](./README.md)
- **Improvements:** [CLI_TESTING_IMPROVEMENTS.md](./CLI_TESTING_IMPROVEMENTS.md)

## ✅ Test Quality Checklist

- [ ] Tests extend `CLITestCase`
- [ ] Descriptive test names
- [ ] Success cases covered
- [ ] Error cases covered
- [ ] Edge cases covered
- [ ] Mocks used for external dependencies
- [ ] Proper cleanup in tearDown
- [ ] Docstrings on test methods

## 🎯 Coverage Goals

- CLI commands: **95%+** ✅
- Core functionality: **90%+** ✅
- Error handling: **100%** ✅
- Runtimes: **85%+** ✅

## 🚦 CI/CD Integration

### GitHub Actions
```yaml
- run: python tests/test_runner.py --coverage
- uses: codecov/codecov-action@v3
```

### Pre-commit Hook
```bash
python tests/test_runner.py --cli --verbose 1
```

## 💡 Quick Tips

1. **Run tests before committing**
2. **Use coverage to find gaps**
3. **Mock external services**
4. **Skip tests when deps unavailable**
5. **Follow existing patterns**

## 🔗 Related Commands

```bash
# Via admin command
fluid admin test

# Quick mode
fluid admin test --quick

# Full test suite
python tests/run_tests.py
```
