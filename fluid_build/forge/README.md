# FLUID Forge System

A modular, extensible project generation system for FLUID Build.

## Overview

The FLUID Forge system replaces the monolithic forge.py with a plugin-based architecture that enables teams to easily extend the system with custom templates, providers, extensions, and generators.

## Architecture

```
fluid_build/forge/
├── core/                    # Core interfaces and engine
│   ├── interfaces.py        # Base interfaces for all components
│   ├── registry.py         # Component registration and discovery
│   └── engine.py           # Main orchestration engine
├── templates/              # Project templates
│   ├── starter.py          # Basic project template
│   ├── analytics.py        # Analytics workflow template
│   ├── ml_pipeline.py      # ML pipeline template
│   ├── etl_pipeline.py     # ETL pipeline template
│   └── streaming.py        # Streaming data template
├── providers/              # Infrastructure providers
│   ├── local.py            # Local development
│   ├── gcp.py              # Google Cloud Platform
│   ├── aws.py              # Amazon Web Services
│   └── snowflake.py        # Snowflake integration
├── extensions/             # Lifecycle extensions
│   ├── project_history.py  # Project tracking
│   ├── environment_validator.py # Environment validation
│   └── ai_assistant.py     # AI-powered assistance
├── generators/             # File generators
│   ├── contract_generator.py # FLUID contract files
│   ├── readme_generator.py  # README files
│   └── config_generator.py  # Configuration files
└── plugins/                # Plugin management
    └── discovery.py        # Auto-discovery system
```

## Key Features

### 🔧 **Extensible Architecture**
- Plugin-based system with clear interfaces
- Auto-discovery of custom components
- Registry pattern for component management

### 📋 **Rich Templates**
- 5 built-in templates covering common use cases
- Template validation and input collection
- Dependency management per template

### ☁️ **Multi-Provider Support**
- Local, GCP, AWS, and Snowflake providers
- Provider-specific configuration generation
- Environment variable management

### 🔌 **Extension System**
- Lifecycle hooks for custom functionality
- Project history tracking
- Environment validation
- AI-powered assistance

### 📄 **File Generation**
- Modular generators for different file types
- Template-aware generation
- Provider-specific configurations

## Quick Start

### Basic Usage

```python
from fluid_build.forge.core.engine import ForgeEngine

# Create a new project
engine = ForgeEngine()
project = engine.create_project_interactive()
```

### CLI Integration

```bash
# Use via existing CLI
fluid forge --template analytics --provider gcp

# Interactive mode
fluid forge --interactive
```

## Built-in Components

### Templates

| Template | Description | Use Case |
|----------|-------------|----------|
| `starter` | Basic project structure | General-purpose projects |
| `analytics` | Analytics workflows | Data analysis, reporting |
| `ml_pipeline` | ML model pipelines | Machine learning projects |
| `etl_pipeline` | ETL workflows | Data transformation |
| `streaming` | Real-time data | Stream processing |

### Providers

| Provider | Description | Services |
|----------|-------------|----------|
| `local` | Local development | File system, local processes |
| `gcp` | Google Cloud | BigQuery, Cloud Storage, Dataflow |
| `aws` | Amazon Web Services | S3, Redshift, Glue, Lambda |
| `snowflake` | Snowflake platform | Data warehouse, streams |

### Extensions

| Extension | Description | Functionality |
|-----------|-------------|---------------|
| `project_history` | Project tracking | Creation history, metadata |
| `environment_validator` | Environment checks | Dependency validation |
| `ai_assistant` | AI assistance | Smart suggestions, help |

### Generators

| Generator | Description | Output |
|-----------|-------------|---------|
| `contract_generator` | FLUID contracts | `contract.fluid.yaml` |
| `readme_generator` | Documentation | `README.md` |
| `config_generator` | Configuration | Config files, scripts |

## Creating Custom Components

### Custom Template

```python
from fluid_build.forge.core.interfaces import ProjectTemplate

class MyTemplate(ProjectTemplate):
    @property
    def name(self) -> str:
        return "my_custom"
    
    def generate_project_structure(self, inputs):
        return {
            "src/main.py": "# My custom template",
            "config.yaml": "custom: true"
        }
```

### Custom Provider

```python
from fluid_build.forge.core.interfaces import InfrastructureProvider

class MyProvider(InfrastructureProvider):
    @property
    def name(self) -> str:
        return "my_cloud"
    
    def generate_deployment_config(self, project_config):
        return DeploymentConfig(
            files={"deploy.yaml": "# My deployment"},
            commands=["my-deploy"]
        )
```

### Registration

```python
from fluid_build.forge.core.registry import get_template_registry

# Register your components
get_template_registry().register(MyTemplate())
```

## Configuration

### Environment Variables

```bash
# Plugin discovery paths
export FLUID_FORGE_PLUGINS_PATH="/path/to/custom/plugins"

# Enable debug mode
export FLUID_FORGE_DEBUG=true
```

### Entry Points

Register plugins via `pyproject.toml`:

```toml
[project.entry-points."fluid_forge.templates"]
my_template = "my_package.templates:MyTemplate"

[project.entry-points."fluid_forge.providers"]
my_provider = "my_package.providers:MyProvider"
```

## Migration from Legacy

The new system maintains backward compatibility:

- Legacy `forge.py` moved to `forge_legacy.py`
- New CLI wrapper preserves existing commands
- Gradual migration path available

### Legacy Support

```python
# Legacy functions still available
from fluid_build.cli.forge_legacy import create_project_legacy

# New system
from fluid_build.forge.core.engine import ForgeEngine
```

## Development

### Testing

```bash
# Run tests
python -m pytest fluid_build/forge/tests/

# Test specific component
python -m pytest fluid_build/forge/tests/test_templates.py
```

### Debugging

```python
import logging
logging.basicConfig(level=logging.DEBUG)

# Enable forge debugging
import os
os.environ['FLUID_FORGE_DEBUG'] = 'true'
```

## Performance

- **Plugin Discovery**: Cached after first load
- **Template Validation**: Lazy validation on demand
- **File Generation**: Streamed for large projects
- **Registry**: In-memory caching with invalidation

## Security

- **Input Validation**: All inputs validated before processing
- **Path Traversal**: Protected against directory traversal
- **Dependency Injection**: Safe component loading
- **Environment Isolation**: Secure environment variable handling

## Roadmap

### Phase 1 ✅ (Current)
- Core architecture implementation
- Built-in templates and providers
- Extension system
- Generator framework

### Phase 2 (Next)
- Advanced template features (inheritance, composition)
- Provider orchestration (multi-cloud deployments)
- Extension marketplace
- AI-powered template suggestions

### Phase 3 (Future)
- Visual template designer
- Real-time collaboration
- Template versioning and migration
- Performance optimization

## Contributing

### Adding Templates

1. Create template class implementing `ProjectTemplate`
2. Add to `fluid_build/forge/templates/`
3. Register in `__init__.py`
4. Add tests
5. Update documentation

### Adding Providers

1. Create provider class implementing `InfrastructureProvider`
2. Add to `fluid_build/forge/providers/`
3. Implement deployment configuration
4. Add environment variables
5. Test integration

### Extension Points

See [EXTENSION_GUIDE.md](EXTENSION_GUIDE.md) for comprehensive guidance on:
- Creating custom components
- Plugin distribution
- Testing strategies
- Best practices

## Support

- **Documentation**: [FLUID Build Docs](https://docs.fluid-forge.io/forge)
- **Examples**: [GitHub Examples](https://github.com/fluid-forge/examples/forge)
- **Community**: [Discord #forge](https://discord.gg/fluid-forge)
- **Issues**: [GitHub Issues](https://github.com/fluid-forge/fluid-forge/issues)

---

*This system represents the future of FLUID project generation - extensible, modular, and designed for team collaboration.*