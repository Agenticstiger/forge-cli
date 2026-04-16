# FLUID Forge Contract GPT Packet

The canonical home for the ChatGPT GPT builder packet is now in the docs repository:

- `forge_docs/docs/advanced/chatgpt-forge-contract-gpt/`

Use that folder for:

- GPT instructions
- conversation starters
- builder checklist
- upload manifest
- few-shot examples
- curated knowledge-pack files

This `forge-cli` repository still owns the source-of-truth implementation artifacts the packet was built from, especially:

- `fluid_build/schemas/fluid-schema-0.7.2.json`
- canonical example contracts in `examples/`
- CLI behavior referenced by validation and planning docs

If you update the GPT packet, update the docs-repo copy and only refresh this repo when the schema or canonical examples change.
