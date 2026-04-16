# FLUID Forge Extension System

Quick reference for the extensible FLUID Forge architecture.

## 🎯 **What is the Extension System?**

The FLUID Forge extension system transforms forge from a monolithic tool into a modular, extensible platform where teams can:

- **Add Custom Templates** for specialized project types
- **Create New Providers** for different infrastructure platforms  
- **Build Extensions** that hook into the project lifecycle
- **Develop Generators** for reusable file creation

## 🏗️ **Architecture Overview**

```
fluid_build/forge/
├── core/                   # Core interfaces and engine
├── templates/             # 5 built-in templates
├── providers/             # 4 infrastructure providers
├── extensions/            # Lifecycle hooks
├── generators/            # File generators
└── plugins/               # Auto-discovery system
```

## 🚀 **Quick Examples**

### Custom Template
```python
from fluid_build.forge.core.interfaces import ProjectTemplate

class MyTemplate(ProjectTemplate):
    @property
    def name(self) -> str:
        return "my_custom"
    
    def generate_project_structure(self, inputs):
        return {
            "src/main.py": "# Custom template",
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
            files={"deploy.yaml": "# Deployment config"},
            commands=["deploy"]
        )
```

### Auto-Registration
```toml
# pyproject.toml
[project.entry-points."fluid_forge.templates"]
my_template = "my_package.templates:MyTemplate"

[project.entry-points."fluid_forge.providers"]  
my_provider = "my_package.providers:MyProvider"
```

## 📚 **Documentation**

- **[Complete Extension Guide](../../docs/docs/guides/extending-forge.md)** - Comprehensive walkthrough
- **[EXTENSION_GUIDE.md](EXTENSION_GUIDE.md)** - Technical reference for developers
- **[README.md](README.md)** - System overview and architecture

## 🔧 **Getting Started**

1. **Read the [Extension Guide](../../docs/docs/guides/extending-forge.md)** for step-by-step instructions
2. **Explore built-in examples** in the templates/ and providers/ directories
3. **Create your first extension** using the provided patterns
4. **Test and distribute** your extensions to your team

## 🎉 **Current Status**

✅ **Core Architecture**: Complete plugin-based system  
✅ **Built-in Components**: 5 templates, 4 providers, 3 extensions, 3 generators  
✅ **Auto-Discovery**: Entry points and environment variable support  
✅ **Documentation**: Comprehensive guides and examples  
✅ **CLI Integration**: Seamless backward compatibility  

The system is **production-ready** and actively used by the FLUID Forge command!