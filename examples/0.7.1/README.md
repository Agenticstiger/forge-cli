# FLUID 0.7.1 Examples

This directory contains example contracts demonstrating FLUID 0.7.1 features:

## 🌍 Sovereignty & Data Residency

**[eu-customer-data-gdpr.yaml](./eu-customer-data-gdpr.yaml)**
- EU-only data residency with GDPR compliance
- Strict enforcement mode (blocks non-EU deployments)
- Example: Customer PII that must stay in Europe

## 🤖 AI/LLM Governance

**[ai-restricted-data.yaml](./ai-restricted-data.yaml)**
- Comprehensive agentPolicy examples
- Model whitelisting (only GPT-4 and Claude-3)
- Use case restrictions (no training/fine-tuning)
- Token limits and retention policies

## ⚙️ Provider Actions

**[provider-actions-workflow.yaml](./provider-actions-workflow.yaml)**
- Declarative orchestration with provider actions
- Dataset provisioning, access grants, scheduled tasks
- Example of multi-step data product deployment

## 🔄 Backward Compatibility

**[backward-compatible.yaml](./backward-compatible.yaml)**
- 0.7.1 contract that also validates with 0.5.7 CLI
- Shows migration path from older versions
- Optional 0.7.1 features added incrementally

## Validation

Test all examples:

```bash
# Basic validation
fluid validate examples/0.7.1/eu-customer-data-gdpr.yaml

# With verbose output
fluid validate examples/0.7.1/ai-restricted-data.yaml --verbose

# Check sovereignty enforcement
fluid validate examples/0.7.1/provider-actions-workflow.yaml --strict
```

## Features Demonstrated

| Feature | Example File | Description |
|---------|--------------|-------------|
| **sovereignty** | eu-customer-data-gdpr.yaml | Jurisdiction constraints, regional limits |
| **agentPolicy** | ai-restricted-data.yaml | AI/LLM usage governance |
| **providerActions** | provider-actions-workflow.yaml | Declarative orchestration |
| **enforcementMode** | eu-customer-data-gdpr.yaml | strict, advisory, audit modes |
| **Backward Compat** | backward-compatible.yaml | Works with 0.5.7 and 0.7.1 |
