# Hello FLUID - Template

**Time to Complete**: 2 minutes  
**Difficulty**: Beginner  
**Tutorial**: [001-hello-fluid.md](../../docs/docs/quickstart/001-hello-fluid.md)

## Overview

This is the simplest possible FLUID contract. Perfect for absolute beginners who want to understand the basic structure of a FLUID data product.

## What You'll Learn

- ✅ FLUID contract anatomy (metadata, outputs, transformations)
- ✅ How to run contracts locally with DuckDB
- ✅ Contract validation workflow
- ✅ Simple SQL transformations

## Quick Start

```bash
# Create project from this template
fluid init my-first-project --template hello-world

# Navigate to project
cd my-first-project

# Validate the contract
fluid validate

# Run locally (uses DuckDB)
fluid apply --local

# Query the results
fluid query "SELECT * FROM hello_message"
```

## Expected Output

```
┌──────────────────────────────────────────────────────┬─────────────────────┐
│ message                                              │ created_at          │
├──────────────────────────────────────────────────────┼─────────────────────┤
│ Hello, FLUID! Welcome to contract-driven data eng...│ 2026-01-19 10:30:45 │
└──────────────────────────────────────────────────────┴─────────────────────┘
```

## Contract Structure

```yaml
apiVersion: fluid.io/v1
kind: DataProduct
metadata:              # Who, what, when
  name: hello-world
  version: 1.0.0
  description: "Your first FLUID data product"
  owner: your-team
  tags: [tutorial]

outputs:               # What you're creating
  - name: hello_message
    materialization: table
    schema: [...]

transformations:       # How to create it
  - name: generate_hello
    output: hello_message
    sql: |
      SELECT 'Hello, FLUID!' AS message
```

## Understanding the Contract

### Metadata Section
Describes **who** owns this data product and **what** it does:
- `name`: Unique identifier for this contract
- `version`: Semantic versioning for your data product
- `description`: Human-readable explanation
- `owner`: Team responsible for maintenance
- `tags`: Categorization for discovery

### Outputs Section
Defines **what** data products you're creating:
- `name`: The table/view name that will be created
- `materialization`: How to store it (`table`, `view`, `incremental`)
- `schema`: Column definitions with types and descriptions

### Transformations Section
Specifies **how** to create the outputs:
- `name`: Name of this transformation step
- `output`: Which output this transformation creates
- `type`: Transformation type (`sql`, `python`, etc.)
- `sql`: The actual SQL code to execute

## Next Steps

Now that you've mastered the basics, try these next:

### 002 - CSV to Data Product (5 minutes)
Learn how to ingest CSV files and transform them:
```bash
fluid init customer-data --template csv-basics
```

### 003 - Multi-Source Joins (8 minutes)
Join multiple data sources together:
```bash
fluid init customer-analytics --template multi-source
```

### 011 - Your First DAG (15 minutes)
Auto-generate an Airflow DAG from your contract:
```bash
fluid init my-pipeline --template first-dag
```

## Customization Ideas

Try modifying the contract to:

1. **Add more columns**:
   ```sql
   SELECT 
     'Hello, FLUID!' AS message,
     'Learn by doing' AS motto,
     42 AS answer_to_everything,
     CURRENT_TIMESTAMP AS created_at
   ```

2. **Add data quality checks**:
   ```yaml
   validations:
     - name: message_not_null
       expression: message IS NOT NULL
   ```

3. **Change materialization**:
   ```yaml
   materialization: view  # Instead of table
   ```

## Troubleshooting

### Error: "FLUID CLI not found"
```bash
# Install FLUID CLI
pip install fluid-forge

# Verify installation
fluid --version
```

### Error: "Contract validation failed"
```bash
# Check YAML syntax
fluid validate --verbose

# Common issue: Indentation must be 2 spaces, not tabs
```

### Error: "Cannot connect to DuckDB"
```bash
# DuckDB is embedded - no setup needed!
# If you see this error, try:
fluid apply --local --clean  # Fresh start
```

## Success Criteria

✅ You can create a project with `fluid init`  
✅ Contract validates without errors  
✅ `fluid apply --local` runs successfully  
✅ You can query the `hello_message` table  
✅ You understand the three main sections of a contract  

## Resources

- 📖 [Full Contract Reference](../../docs/docs/cli/init.md)
- 🎓 [Quickstart Tutorial](../../docs/docs/quickstart/001-hello-fluid.md)
- 💬 [Community Forum](https://community.fluiddata.io)
- 🐛 [Report Issues](https://github.com/yourusername/fluid-mono/issues)

---

**Congratulations!** 🎉 You've created your first FLUID data product. Time to level up with more advanced templates!
