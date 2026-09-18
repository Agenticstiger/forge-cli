# Testing Your Contract - Build with Confidence

**Time**: 8 min | **Difficulty**: Intermediate | **Track**: Foundation

## Overview

Write comprehensive tests for FLUID contracts with unit tests, integration tests, and test fixtures. Build confidence through test-driven development.

## Quick Start

```bash
fluid init my-tested-pipeline --template testing-your-contract
cd my-tested-pipeline
fluid test contract.fluid.yaml  # Run all tests
```

## Test Types

### Unit Tests (tests/test_orders.yaml)
```yaml
tests:
  - name: test_revenue_calculation
    model: order_revenue
    given:
      raw_orders:
        - {order_id: 1, amount: 100.00, status: 'completed'}
    expect:
      - {order_id: 1, revenue: 100.00}
```

### Integration Tests
```bash
fluid test contract.fluid.yaml  # Test full pipeline
```

### Validation Tests
```bash
fluid test contract.fluid.yaml  # Run all validations
```

## Test Patterns

### Data-Driven Tests
```yaml
- name: test_status_filter
  given:
    raw_orders:
      - {order_id: 1, status: 'completed'}  # Should include
      - {order_id: 2, status: 'pending'}    # Should exclude
  expect:
    row_count: 1
```

### Edge Cases
```yaml
- name: test_empty_input
  given:
    raw_orders: []
  expect:
    row_count: 0

- name: test_null_handling
  given:
    raw_orders:
      - {order_id: 1, amount: null}
  expect:
    validation_error: true
```

## Running Tests

```bash
# Run the contract's quality rules against live data
fluid test contract.fluid.yaml

# Structure-only (skips the live-data checks)
fluid test contract.fluid.yaml --no-data

# Fail on warnings too
fluid test contract.fluid.yaml --strict
```

## Success Criteria

- [ ] Test files created in tests/ directory
- [ ] Unit tests pass
- [ ] Edge cases covered
- [ ] Validations tested

## Next Steps

- **010-contract-documentation**: Document tested contracts
- **012-pipeline-orchestration**: Test orchestration logic

**Pro Tip**: Write tests before implementations (TDD) to catch bugs early.
