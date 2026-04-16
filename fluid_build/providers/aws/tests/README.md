# AWS Provider Integration Tests

Comprehensive integration test suite for the AWS provider, covering S3, Glue, Athena, and Kinesis operations.

## Test Coverage

### TestS3Integration (4 tests)
- **test_ensure_bucket_creates_new_bucket**: Verifies bucket creation when it doesn't exist
- **test_ensure_bucket_is_idempotent**: Ensures idempotent bucket operations
- **test_upload_file_to_s3**: Tests file upload to S3
- **test_list_objects_in_bucket**: Validates object listing functionality

### TestGlueIntegration (4 tests)
- **test_ensure_database_creates_new_database**: Creates Glue database
- **test_ensure_database_is_idempotent**: Verifies database idempotency
- **test_create_table_with_columns**: Tests table creation with schema
- **test_update_table_partition**: Validates partition management

### TestAthenaIntegration (3 tests)
- **test_execute_query**: Executes synchronous Athena query
- **test_execute_query_async**: Tests asynchronous query execution
- **test_get_query_results**: Retrieves and validates query results

### TestKinesisIntegration (3 tests)
- **test_ensure_stream_creates_new_stream**: Creates Kinesis Data Stream
- **test_put_records_to_stream**: Tests record ingestion
- **test_ensure_analytics_application**: Creates Kinesis Analytics application

### TestAWSProviderEndToEnd (1 test)
- **test_data_pipeline_workflow**: Complete 5-step data pipeline:
  1. Create S3 bucket
  2. Create Glue database
  3. Create Glue table
  4. Upload data to S3
  5. Query with Athena

## Running Tests

### With pytest
```bash
pytest fluid_build/providers/aws/tests/test_integration.py -v
```

### With unittest
```bash
python3 fluid_build/providers/aws/tests/test_integration.py
```

### Run specific test class
```bash
pytest fluid_build/providers/aws/tests/test_integration.py::TestS3Integration -v
```

### Run specific test method
```bash
pytest fluid_build/providers/aws/tests/test_integration.py::TestS3Integration::test_ensure_bucket_creates_new_bucket -v
```

## Test Design

### Mocking Strategy
All tests use `@patch('boto3.client')` to mock AWS SDK calls. This approach:
- ✅ Doesn't require AWS credentials
- ✅ Doesn't create actual AWS resources
- ✅ Runs quickly and reliably
- ✅ Tests provider logic without network calls

### Assertion Coverage
- **29 assertEqual**: Value equality checks
- **6 assertTrue**: Boolean assertions
- **2 assertFalse**: Negative boolean assertions
- **11 assert_called_once**: Method invocation verification
- **3 assert_not_called**: Non-invocation verification

### Key Test Patterns

#### Idempotency Testing
```python
# First call creates resource
result = ensure_bucket(action)
assert result["status"] == "changed"

# Second call is idempotent
result = ensure_bucket(action)
assert result["status"] == "ok"
assert not result["changed"]
```

#### Error Handling
```python
from botocore.exceptions import ClientError

mock_client.get_database.side_effect = ClientError(
    {"Error": {"Code": "EntityNotFoundException"}},
    "GetDatabase"
)
```

#### Resource Creation Verification
```python
result = ensure_database(action)
assert result["status"] == "changed"
mock_glue.create_database.assert_called_once()
```

## Test Requirements

- Python 3.7+
- unittest (standard library)
- unittest.mock (standard library)

Optional:
- pytest (for better test output)
- boto3 (will be mocked, but botocore.exceptions needed)

## Notes

These tests validate:
1. **Correct AWS API calls** - Ensures proper boto3 client usage
2. **Idempotent operations** - Multiple runs produce same result
3. **Error handling** - Graceful handling of AWS errors
4. **Input validation** - Required parameters checked
5. **Return value structure** - Consistent response format

The tests do **not** validate:
- Actual AWS resource creation (use mocks)
- AWS permissions or IAM policies
- Network connectivity to AWS
- AWS service quotas or limits

For end-to-end AWS testing, use separate integration tests with actual AWS credentials and resources.
