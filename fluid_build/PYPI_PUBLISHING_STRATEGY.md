# FLUID Build Strategy - PyPI Publishing

## Overview

Three-stage build pipeline with smart PyPI versioning:

- **Alpha** (0.7.1a{build}) - Always runs, bleeding edge
- **Beta** (0.7.1b{build}) - Always runs regardless of test results
- **Stable** (0.7.1) - Only runs when test coverage ≥40%

## Build Number Strategy

Every build run increments a single global build counter stored in `.build_number`.

**Example Run #42:**
- Alpha: `0.7.1a42`
- Beta: `0.7.1b42`
- Stable: `0.7.1` (only if coverage ≥40%)

**Example Run #43:**
- Alpha: `0.7.1a43`
- Beta: `0.7.1b43`
- Stable: `0.7.1` (skipped if coverage <40%)

## PyPI Folder Structure

```
pypi/
├── alpha/
│   ├── fluid-0.7.1a42-py3-none-any.whl
│   ├── fluid-0.7.1a43-py3-none-any.whl
│   └── build-metadata/
│       ├── 0.7.1a42.json
│       └── 0.7.1a43.json
├── beta/
│   ├── fluid-0.7.1b42-py3-none-any.whl
│   ├── fluid-0.7.1b43-py3-none-any.whl
│   └── build-metadata/
│       ├── 0.7.1b42.json
│       └── 0.7.1b43.json
└── stable/
    ├── fluid-0.7.1-py3-none-any.whl
    └── build-metadata/
        └── 0.7.1.json
```

## Usage

### Publish All Builds (Recommended)
```bash
cd forge-cli/fluid_build
./publish-to-pypi.sh all
```

This will:
1. Increment build number (e.g., 42 → 43)
2. Build and publish alpha (0.7.1a43)
3. Build and publish beta (0.7.1b43)
4. Check test coverage
5. Conditionally publish stable (0.7.1) if coverage ≥80%

### Publish Single Build Type
```bash
./publish-to-pypi.sh alpha   # Publish only alpha
./publish-to-pypi.sh beta    # Publish only beta
./publish-to-pypi.sh stable  # Publish only stable
```

## Build Metadata

Each build creates a JSON metadata file with:
- Version
- Build number
- Timestamp
- Git commit/branch
- Build host

Example: `pypi/alpha/build-metadata/0.7.1a42.json`
```json
{
  "version": "0.7.1a42",
  "build_type": "alpha",
  "build_number": 42,
  "timestamp": "2026-01-20T16:34:12Z",
  "git_commit": "abc123...",
  "git_branch": "main",
  "built_by": "jenkins",
  "hostname": "build-server"
}
```

## CI/CD Integration

### Jenkins Pipeline
```groovy
stage('Build All') {
    steps {
        sh 'cd forge-cli/fluid_build && ./publish-to-pypi.sh all'
    }
}
```

### GitHub Actions
```yaml
- name: Publish to PyPI
  run: |
    cd forge-cli/fluid_build
    ./publish-to-pypi.sh all
```

## Version Bumping

To bump the base version (e.g., 0.7.1 → 0.7.2):

1. Edit [build-manifest.yaml](build-manifest.yaml):
   ```yaml
   pypi:
     base_version: "0.7.2"  # Changed from 0.7.1
   ```

2. Next build will produce:
   - Alpha: `0.7.2a44`
   - Beta: `0.7.2b44`
   - Stable: `0.7.2`

## Benefits

✅ **Three builds per run** - Alpha, Beta, and (conditionally) Stable
✅ **Single build counter** - Easy to correlate builds from same run
✅ **Quality gate** - Stable only publishes when tests pass
✅ **Beta always ships** - Get feedback even when tests fail
✅ **Clear naming** - Instantly identify build type and number
✅ **Full traceability** - Metadata tracks every build detail
