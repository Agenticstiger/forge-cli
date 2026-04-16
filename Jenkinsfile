// Jenkinsfile for FLUID CLI Package Building
// Builds Python wheel on every commit and stores in artifact branch

pipeline {
    agent any
    
    environment {
        // Package info
        PACKAGE_NAME = 'fluid-forge'
        PACKAGE_DIR = '.'
        
        // Infrastructure config - sourced from Jenkins parameters (set via Build with Parameters)
        // First run: click "Build with Parameters" and fill in your NAS details
        // Subsequent runs: Jenkins remembers the last values
        NAS_HOST = "${params.NAS_HOST}"
        NAS_SSH_USER = "${params.NAS_SSH_USER}"
        PYPI_PORT = "${params.PYPI_PORT}"
        
        // Artifact storage - SEPARATE REPOSITORY (constructed from params)
        ARTIFACT_REPO = "ssh://${params.NAS_SSH_USER}@${params.NAS_HOST}/volume1/git-server/fluid-cli-builds.git"
        ARTIFACT_DIR = 'builds'
        
        // Source Git config
        GIT_SERVER = "ssh://${params.NAS_SSH_USER}@${params.NAS_HOST}/volume1/git-server/dustlabs/at/fluid/fluid-forge-cli.git"
        SDK_REPO = "ssh://${params.NAS_SSH_USER}@${params.NAS_HOST}/volume1/git-server/dustlabs/at/fluid/fluid-provider-sdk.git"
        
        // PyPI config (constructed from params)
        PYPI_URL = "http://${params.NAS_HOST}:${params.PYPI_PORT}"
        PYPI_SIMPLE_URL = "http://${params.NAS_HOST}:${params.PYPI_PORT}/simple"
        
        // Docker registry
        DOCKER_REGISTRY = "${params.DOCKER_REGISTRY}"
        DOCKER_IMAGE = "${params.DOCKER_REGISTRY}/fluid-forge-cli"
        
        // Python config
        PYTHON_VERSION = '3.11'
        
    }
    
    options {
        buildDiscarder(logRotator(numToKeepStr: '30'))
        timestamps()
        timeout(time: 30, unit: 'MINUTES')
    }
    
    stages {
        stage('Validate Parameters') {
            steps {
                script {
                    if (!params.NAS_HOST?.trim()) {
                        currentBuild.result = 'NOT_BUILT'
                        currentBuild.description = 'First run — parameters registered. Re-run with "Build with Parameters".'
                        echo """
╔══════════════════════════════════════════════════════════════╗
║  ✅ PIPELINE REGISTERED — Parameters are now available       ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  This was a first-run scan. Jenkins has now loaded the       ║
║  pipeline parameters.                                        ║
║                                                              ║
║  NEXT STEP: Click "Build with Parameters" and set:           ║
║    • NAS_HOST         → Your NAS IP (e.g. 192.168.1.100)    ║
║    • NAS_SSH_USER     → SSH user on the NAS                  ║
║    • PYPI_PORT        → Private PyPI port (default: 8080)    ║
║    • DOCKER_REGISTRY  → Docker registry (default: localhost) ║
║                                                              ║
║  Jenkins will remember these values for future builds.       ║
╚══════════════════════════════════════════════════════════════╝
"""
                        // Stop the entire pipeline gracefully (grey status, not red)
                        error('First-run parameter registration complete. Re-run with "Build with Parameters".')
                    }
                    echo "✅ Infrastructure config: NAS=${params.NAS_HOST}, User=${params.NAS_SSH_USER}, PyPI port=${params.PYPI_PORT}, Docker=${params.DOCKER_REGISTRY}"
                }
            }
        }
        
        stage('Clean Workspace') {
            steps {
                echo "🧹 Cleaning workspace for fresh build..."
                cleanWs()
                
                // Checkout fresh code
                checkout scm
                
                echo "✅ Fresh workspace ready"
            }
        }
        
        stage('Setup Python Environment') {
            steps {
                script {
                    // Multi-Profile Pipeline Strategy
                    // Check if this is a promoted build (override profile)
                    if (params.OVERRIDE_PROFILE) {
                        env.BUILD_PROFILE = params.OVERRIDE_PROFILE
                        env.PROFILES_TO_BUILD = params.OVERRIDE_PROFILE
                        env.INITIAL_PROFILE = params.OVERRIDE_PROFILE
                        
                        echo "🔄 PROMOTED BUILD from ${params.PARENT_BUILD}"
                        echo "🎯 Building forced profile: ${params.OVERRIDE_PROFILE}"
                    } else {
                        // Normal branch-based detection
                        def branchName = env.BRANCH_NAME ?: 'unknown'
                        
                        // Determine which profiles to build in cascade
                        if (branchName == 'main') {
                            env.PROFILES_TO_BUILD = 'experimental,stable'
                            env.INITIAL_PROFILE = 'experimental'
                        } else if (branchName.startsWith('release/beta')) {
                            env.PROFILES_TO_BUILD = 'experimental,beta,stable'
                            env.INITIAL_PROFILE = 'experimental'
                        } else {
                            // Feature branches build all profiles
                            env.PROFILES_TO_BUILD = 'experimental,alpha,beta,stable'
                            env.INITIAL_PROFILE = 'experimental'
                        }
                        
                        env.BUILD_PROFILE = env.INITIAL_PROFILE
                        
                        echo "🔧 Multi-Profile Cascade Build"
                        echo "📦 Branch: ${branchName}"
                        echo "🎯 Profile cascade: ${env.PROFILES_TO_BUILD}"
                        echo "▶️  Starting with: ${env.BUILD_PROFILE}"
                    }
                }
                
                echo "Setting up Python environment"
                sh '''
                    cd ${PACKAGE_DIR}
                    
                    # Find available Python 3 by actually testing it
                    PYTHON_CMD=""
                    for py in python3.11 python3.10 python3.9 python3.8 python3; do
                        if $py --version >/dev/null 2>&1; then
                            PYTHON_CMD=$py
                            echo "Found working Python: $PYTHON_CMD"
                            break
                        fi
                    done
                    
                    if [ -z "$PYTHON_CMD" ]; then
                        echo "ERROR: Python 3.8+ not found!"
                        echo "Tried: python3.11, python3.10, python3.9, python3.8, python3"
                        exit 1
                    fi
                    
                    echo "Using Python: $PYTHON_CMD ($($PYTHON_CMD --version 2>&1))"
                    
                    # Create virtual environment (try venv first, fallback to virtualenv)
                    if $PYTHON_CMD -m venv .venv 2>/dev/null; then
                        echo "Created venv successfully"
                    elif $PYTHON_CMD -m pip install --user --break-system-packages virtualenv 2>/dev/null && $PYTHON_CMD -m virtualenv .venv; then
                        echo "Created virtualenv successfully"
                    else
                        echo "ERROR: Could not create virtual environment"
                        echo "Installing python3-venv on Jenkins server..."
                        echo "Please run: sudo apt install python3-venv python3-pip"
                        exit 1
                    fi
                    
                    # Activate and upgrade pip
                    . .venv/bin/activate
                    pip install --upgrade pip setuptools wheel
                    
                    # Install build tools and feature release dependencies
                    pip install build twine pyyaml
                '''
            }
        }
        
        stage('Publish SDK to Private PyPI') {
            steps {
                echo "📦 Ensuring fluid-provider-sdk is available"
                withCredentials([usernamePassword(credentialsId: 'pypi-server-credentials',
                                                   usernameVariable: 'PYPI_USER',
                                                   passwordVariable: 'PYPI_PASS'),
                                 sshUserPrivateKey(credentialsId: 'khyana-synology-git-ssh',
                                                   keyFileVariable: 'SSH_KEY')]) {
                    sh '''
                        export GIT_SSH_COMMAND="ssh -i ${SSH_KEY} -o StrictHostKeyChecking=no"
                        
                        echo "Building fluid-provider-sdk from source..."
                        SDK_DIR=$(mktemp -d)
                        
                        git clone --depth 1 ${SDK_REPO} "${SDK_DIR}/fluid-provider-sdk" || {
                            echo "❌ Could not clone SDK repo from ${SDK_REPO}"
                            echo "Ensure the repo exists on your git server."
                            exit 1
                        }
                        
                        cd "${SDK_DIR}/fluid-provider-sdk"
                        . ${WORKSPACE}/${PACKAGE_DIR}/.venv/bin/activate
                        python -m build
                        
                        # Install SDK wheel directly into the build venv
                        pip install dist/fluid_provider_sdk-*.whl
                        
                        # Save a copy of the wheel for the test venv later
                        mkdir -p ${WORKSPACE}/.sdk-wheels
                        cp dist/fluid_provider_sdk-*.whl ${WORKSPACE}/.sdk-wheels/
                        
                        # Also upload to private PyPI (best-effort)
                        twine upload \
                            --repository-url ${PYPI_URL} \
                            -u "${PYPI_USER}" -p "${PYPI_PASS}" \
                            dist/*.whl dist/*.tar.gz || {
                            echo "⚠️  twine upload returned non-zero — package may already exist"
                        }
                        
                        cd ${WORKSPACE}
                        rm -rf "${SDK_DIR}"
                        echo "✅ fluid-provider-sdk installed into venv and uploaded to ${PYPI_URL}"
                    '''
                }
            }
        }
        
        stage('Feature Status Check') {
            steps {
                echo "📋 Checking feature status for ${BUILD_PROFILE} profile"
                sh '''
                    cd ${PACKAGE_DIR}
                    . .venv/bin/activate
                    
                    # Set build profile
                    export FLUID_BUILD_PROFILE=${BUILD_PROFILE}
                    
                    echo "═══════════════════════════════════════════════════════"
                    echo "  Build Profile: ${BUILD_PROFILE}"
                    echo "  Branch: ${GIT_BRANCH}"
                    echo "═══════════════════════════════════════════════════════"
                    
                    # Show feature status
                    python scripts/check_features.py
                    
                    # Export build metadata for later stages
                    cat > export_metadata.py << 'PYTHON_SCRIPT'
import fluid_build
import json

summary = fluid_build.get_features_summary()

print()
print("=== Build Summary ===")
print("Profile: {}".format(summary["profile"]))
print("Providers: {}".format(", ".join(summary["providers"])))
print("Provider Count: {}".format(summary["provider_count"]))
print("Command Count: {}".format(summary["command_count"]))

# Export to file for artifact metadata
with open('build_metadata.json', 'w') as f:
    json.dump(summary, f, indent=2)

# Export to env file for Jenkins
with open('build_profile.env', 'w') as f:
    f.write("PROFILE={}\\n".format(summary["profile"]))
    f.write("PROVIDER_COUNT={}\\n".format(summary["provider_count"]))
    f.write("COMMAND_COUNT={}\\n".format(summary["command_count"]))
    f.write("PROVIDERS={}\\n".format(",".join(summary["providers"])))
PYTHON_SCRIPT

                    python export_metadata.py
                    rm export_metadata.py
                    
                    # Show exported metadata
                    echo ""
                    echo "=== Exported Metadata ==="
                    cat build_profile.env
                    echo ""
                '''
                
                // Load metadata into environment variables
                script {
                    def propsFile = readFile("build_profile.env").trim()
                    propsFile.split("\n").each { line ->
                        if (line.trim() && line.contains('=')) {
                            def parts = line.split('=', 2)
                            def key = parts[0].trim()
                            def value = parts[1].trim()
                            
                            // Use individual assignments instead of env[key] which is blocked by sandbox
                            if (key == 'PROFILE') {
                                env.PROFILE = value
                            } else if (key == 'PROVIDER_COUNT') {
                                env.PROVIDER_COUNT = value
                            } else if (key == 'COMMAND_COUNT') {
                                env.COMMAND_COUNT = value
                            } else if (key == 'PROVIDERS') {
                                env.PROVIDERS = value
                            }
                        }
                    }
                    echo "✅ Loaded: ${env.PROVIDER_COUNT} providers (${env.PROVIDERS}), ${env.COMMAND_COUNT} commands"
                }
            }
        }
        
        stage('Provider Quality Assessment') {
            steps {
                echo "🔍 Assessing provider quality for ${BUILD_PROFILE} profile"
                sh '''
                    cd ${PACKAGE_DIR}
                    . .venv/bin/activate
                    
                    echo "════════════════════════════════════════════════════════════════════════"
                    echo "  Provider Quality Assessment"
                    echo "════════════════════════════════════════════════════════════════════════"
                    
                    # Get list of enabled providers for this build profile
                    export FLUID_BUILD_PROFILE=${BUILD_PROFILE}
                    ENABLED_PROVIDERS=$(python3 -c "
import fluid_build
providers = fluid_build.get_enabled_providers()
print(' '.join(providers))
")
                    
                    echo "Enabled providers for ${BUILD_PROFILE}: $ENABLED_PROVIDERS"
                    echo ""
                    
                    # Determine quality level based on build profile
                    case "${BUILD_PROFILE}" in
                        stable)
                            QUALITY_LEVEL="stable"
                            STRICT_MODE="--strict"
                            ;;
                        beta)
                            QUALITY_LEVEL="beta"
                            STRICT_MODE=""
                            ;;
                        alpha)
                            QUALITY_LEVEL="alpha"
                            STRICT_MODE=""
                            ;;
                        *)
                            QUALITY_LEVEL="alpha"
                            STRICT_MODE=""
                            ;;
                    esac
                    
                    echo "Quality level: $QUALITY_LEVEL"
                    echo "Strict mode: ${STRICT_MODE:-disabled}"
                    echo ""
                    
                    # Assess each enabled provider
                    ASSESSMENT_FAILED=0
                    for provider in $ENABLED_PROVIDERS; do
                        echo "\\n--- Assessing: $provider ---"
                        if python3 scripts/assess_provider.py --provider $provider --level $QUALITY_LEVEL $STRICT_MODE; then
                            echo "✅ $provider meets $QUALITY_LEVEL criteria"
                        else
                            echo "⚠️  $provider does not fully meet $QUALITY_LEVEL criteria"
                            if [ "${BUILD_PROFILE}" = "stable" ]; then
                                ASSESSMENT_FAILED=1
                            fi
                        fi
                    done
                    
                    # Fail build if stable and any provider failed
                    if [ $ASSESSMENT_FAILED -eq 1 ]; then
                        echo "\\n❌ QUALITY GATE FAILED: One or more providers do not meet stable criteria"
                        exit 1
                    fi
                    
                    echo "\\n✅ All provider quality checks passed for ${BUILD_PROFILE} profile"
                '''
            }
        }
        
        stage('Run Tests') {
            steps {
                echo "🧪 Running test suite with coverage analysis"
                sh '''
                    cd ${PACKAGE_DIR}
                    . .venv/bin/activate
                    
                    # Install package with test dependencies
                    pip install -e ".[dev,test]" pytest-cov pytest-json-report
                    
                    # Run tests with coverage (if they exist)
                    if [ -d "tests" ]; then
                        echo "═══════════════════════════════════════════════════════"
                        echo "  Running Tests with Coverage Analysis"
                        echo "  Profile: ${BUILD_PROFILE}"
                        echo "═══════════════════════════════════════════════════════"
                        
                        # Run ALL tests with coverage
                        # Provider coverage will be extracted from the overall coverage.json
                        pytest tests/ \
                            --cov=fluid_build \
                            --cov-report=term \
                            --cov-report=json:coverage.json \
                            --cov-report=html:htmlcov \
                            --json-report \
                            --json-report-file=test-report.json \
                            --maxfail=5 \
                            --disable-warnings \
                            -v || TEST_EXIT_CODE=$?
                        
                        # Always show coverage summary
                        if [ -f "coverage.json" ]; then
                            echo ""
                            echo "═══════════════════════════════════════════════════════"
                            echo "  Coverage Report"
                            echo "═══════════════════════════════════════════════════════"
                            python3 -c "
import json
with open('coverage.json') as f:
    cov = json.load(f)
    total_cov = cov['totals']['percent_covered']
    print(f'Overall Coverage: {total_cov:.1f}%')
    print()
    print('Per-File Coverage:')
    for file, data in sorted(cov['files'].items()):
        if 'fluid_build' in file:
            pct = data['summary']['percent_covered']
            print(f'  {file}: {pct:.1f}%')
"
                        fi
                        
                        # Store test exit code for later quality gates
                        # Don't exit here - we want to generate reports even if tests fail
                        echo "${TEST_EXIT_CODE:-0}" > test-exit-code.txt
                        echo "Test exit code: ${TEST_EXIT_CODE:-0} (saved for quality gates)"
                    else
                        echo "⚠️  No tests found, skipping test execution..."
                        echo "WARNING: Building without test coverage data"
                        
                        # Create empty coverage file for downstream stages
                        echo '{"totals": {"percent_covered": 0}, "files": {}}' > coverage.json
                        echo '{"summary": {"passed": 0, "failed": 0, "total": 0}}' > test-report.json
                        echo "0" > test-exit-code.txt
                    fi
                '''
                
                // Generate test report for risk assessment
                sh '''
                    cd ${PACKAGE_DIR}
                    . .venv/bin/activate
                    
                    echo ""
                    echo "═══════════════════════════════════════════════════════"
                    echo "  Generating Test Coverage Report"
                    echo "═══════════════════════════════════════════════════════"
                    
                    # Generate report for current profile
                    python scripts/generate_test_report.py ${BUILD_PROFILE}
                    
                    # Display summary
                    if [ -f build-test-report.md ]; then
                        echo ""
                        echo "📊 Test Report Summary:"
                        head -n 30 build-test-report.md
                    fi
                '''
                
                // Parse test results and coverage using Python (readJSON not available)
                sh '''
                    cd ${PACKAGE_DIR}
                    
                    # Extract coverage percentage
                    if [ -f coverage.json ]; then
                        python3 -c "import json; data=json.load(open('coverage.json')); print(data['totals']['percent_covered'])" > coverage-pct.txt
                        echo "📊 Overall Test Coverage: $(cat coverage-pct.txt)%"
                    else
                        echo "0" > coverage-pct.txt
                        echo "⚠️  No coverage data available"
                    fi
                    
                    # Extract test counts
                    # Note: pytest-json-report omits keys with 0 count, so use .get() with defaults
                    if [ -f test-report.json ]; then
                        python3 -c "import json; s=json.load(open('test-report.json')).get('summary',{}); print(s.get('passed',0))" > tests-passed.txt
                        python3 -c "import json; s=json.load(open('test-report.json')).get('summary',{}); print(s.get('failed',0))" > tests-failed.txt
                        python3 -c "import json; s=json.load(open('test-report.json')).get('summary',{}); print(s.get('total', s.get('collected',0)))" > tests-total.txt
                        echo "✅ Tests: $(cat tests-passed.txt)/$(cat tests-total.txt) passed"
                    else
                        echo "0" > tests-passed.txt
                        echo "0" > tests-failed.txt
                        echo "0" > tests-total.txt
                    fi
                '''
                
                script {
                    env.OVERALL_COVERAGE = readFile("coverage-pct.txt").trim()
                    env.TESTS_PASSED = readFile("tests-passed.txt").trim()
                    env.TESTS_FAILED = readFile("tests-failed.txt").trim()
                    env.TESTS_TOTAL = readFile("tests-total.txt").trim()
                }
            }
        }
        
        stage('Build Package') {
            steps {
                echo "🏗️  Building Python wheel with ${BUILD_PROFILE} profile"
                sh '''
                    cd ${PACKAGE_DIR}
                    . .venv/bin/activate
                    
                    # Set build profile environment variable
                    export FLUID_BUILD_PROFILE=${BUILD_PROFILE}
                    
                    echo "═══════════════════════════════════════════════════════"
                    echo "  Building with Profile: ${BUILD_PROFILE}"
                    echo "  Providers: ${PROVIDER_COUNT}"
                    echo "  Commands: ${COMMAND_COUNT}"
                    echo "═══════════════════════════════════════════════════════"
                    
                    # CRITICAL: Uninstall any previously installed version
                    echo "🧹 Uninstalling any existing fluid-forge package..."
                    pip uninstall -y fluid-forge 2>/dev/null || echo "No previous installation found"
                    
                    # FORCE CLEAN BUILD - remove ALL cached files
                    echo "🧹 Removing all cached build artifacts..."
                    rm -rf dist/ build/ *.egg-info
                    rm -rf fluid_build/*.pyc fluid_build/__pycache__
                    find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
                    find . -type f -name '*.pyc' -delete 2>/dev/null || true
                    find . -type f -name '*.pyo' -delete 2>/dev/null || true
                    
                    # Clear Python import cache
                    echo "🧹 Clearing Python import cache..."
                    python3 -c "import sys; sys.path.insert(0, '.'); import importlib; importlib.invalidate_caches()"
                    
                    # Build wheel and source distribution
                    python -m build
                    
                    # List created files
                    echo "=== Built Packages ==="
                    ls -lh dist/
                    
                    # Verify wheel
                    twine check dist/*.whl
                    
                    # Verify profile is correctly set
                    echo "\n=== Verifying Build Profile ==="
                    python3 -c "import fluid_build; assert fluid_build.get_build_profile() == '${BUILD_PROFILE}', 'Profile mismatch!'; print('✅ Profile verified: ${BUILD_PROFILE}')"
                    
                    # CRITICAL: Save version info NOW before PyPI publishing overwrites dist/
                    # Extract version from the wheel we just built
                    WHEEL_FILE=$(ls dist/*.whl | head -1)
                    BUILT_VERSION=$(basename "$WHEEL_FILE" | sed 's/fluid_forge-//;s/-py3.*//')
                    echo "$BUILT_VERSION" > built-version.txt
                    echo "📦 Saved built version for Docker stage: $BUILT_VERSION"
                '''
            }
        }
        
        stage('Quality Gates') {
            when {
                expression { env.BUILD_PROFILE in ['stable', 'beta'] }
            }
            steps {
                echo "🚦 Enforcing quality gates for ${BUILD_PROFILE} profile with test data"
                sh '''
                    cd ${PACKAGE_DIR}
                    . .venv/bin/activate
                    
                    echo "═══════════════════════════════════════════════════════"
                    echo "  Quality Gate Validation: ${BUILD_PROFILE}"
                    echo "═══════════════════════════════════════════════════════"
                    
                    # Validate with actual test results and coverage
                    python3 << "QUALITY_GATES_EOF"
import yaml
import sys

# Load build manifest (simple version)
manifest = yaml.safe_load(open('fluid_build/build-manifest.yaml'))

print('\\nBuild Manifest for ${BUILD_PROFILE}:')
build_config = manifest['builds']['${BUILD_PROFILE}']
print(f"  Description: {build_config['description']}")
print(f"  Commands: {len(build_config.get('commands', []))}")
print(f"  Providers: {len(build_config.get('providers', []))}")

# Load actual test results
coverage_pct = float('${OVERALL_COVERAGE}' or '0')
tests_passed = int('${TESTS_PASSED}' or '0')
tests_failed = int('${TESTS_FAILED}' or '0')
tests_total = int('${TESTS_TOTAL}' or '0')

print(f'\\n📊 Actual Test Results:')
print(f'  Tests: {tests_passed}/{tests_total} passed ({tests_failed} failed)')
print(f'  Coverage: {coverage_pct:.1f}%')

# Check if tests actually failed (exit code != 0)
test_exit_code = 0
try:
    with open('test-exit-code.txt') as f:
        test_exit_code = int(f.read().strip())
except:
    pass

if test_exit_code != 0:
    print(f'\\n⚠️  Tests exited with code {test_exit_code}')

# Manifest-driven quality gates (no coverage thresholds)
# You control what's packaged via build-manifest.yaml
# This only checks: do tests pass?
if '${BUILD_PROFILE}' == 'stable':
    print('\\n🔒 STABLE - Quality Check')
    
    if tests_failed > 0:
        print(f'  ❌ FAILED: {tests_failed} tests failing')
        sys.exit(1)
    print(f'  ✅ All {tests_total} tests passing')
    print(f'  ℹ️  Coverage: {coverage_pct:.1f}% (review build-test-report.md)')
        
elif '${BUILD_PROFILE}' == 'beta':
    print('\\n⚠️  BETA - Quality Check')
    
    if tests_failed > 0:
        print(f'  ❌ FAILED: {tests_failed} tests failing')
        sys.exit(1)
    print(f'  ✅ All {tests_total} tests passing')
    print(f'  ℹ️  Coverage: {coverage_pct:.1f}% (review build-test-report.md)')
        
else:
    # Alpha - just report
    print('\\n🔧 ALPHA - Quality Check')
    if tests_failed > 0:
        print(f'  ⚠️  {tests_failed} test failures (not blocking alpha)')
    else:
        print(f'  ✅ All {tests_total} tests passing')
    print(f'  ℹ️  Coverage: {coverage_pct:.1f}% (review build-test-report.md)')

print('\\n✅ Quality checks complete for ${BUILD_PROFILE}')
QUALITY_GATES_EOF
                    
                    # Test package installation
                    echo ""
                    echo "═══════════════════════════════════════════════════════"
                    echo "  Package Installation Verification"
                    echo "═══════════════════════════════════════════════════════"
                    TEST_VENV=/tmp/test-fluid-${BUILD_NUMBER}
                    python3 -m venv $TEST_VENV
                    $TEST_VENV/bin/pip install --upgrade pip -q
                    $TEST_VENV/bin/pip install ${WORKSPACE}/.sdk-wheels/fluid_provider_sdk-*.whl -q
                    $TEST_VENV/bin/pip install dist/*.whl -q
                    
                    # Verify installed package works
                    $TEST_VENV/bin/python3 -c "import fluid_build; print(f'✅ Version: {fluid_build.__version__}')"
                    $TEST_VENV/bin/python3 -c "import fluid_build; print(f'✅ Providers: {list(fluid_build.get_enabled_providers())}')"
                    
                    # Cleanup
                    rm -rf $TEST_VENV
                    
                    echo ""
                    echo "✅ All quality gates PASSED for ${BUILD_PROFILE}"
                '''
            }
        }
        
        stage('Determine Next Profile') {
            steps {
                script {
                    // Determine if we should build next profile
                    def currentProfile = env.BUILD_PROFILE
                    def profilesList = env.PROFILES_TO_BUILD.split(',')
                    def currentIndex = profilesList.findIndexOf { it == currentProfile }
                    
                    echo "📍 Current profile: ${currentProfile} (index ${currentIndex} of ${profilesList.size()})"
                    
                    if (currentIndex >= 0 && currentIndex < profilesList.size() - 1) {
                        def failed = (env.TESTS_FAILED ?: '0') as Integer
                        def nextProfile = profilesList[currentIndex + 1]
                        
                        // Manifest-driven promotion: tests pass = cascade continues
                        // NO thresholds - you control risk via build-manifest.yaml
                        // Review build-test-report.md to decide what to include
                        if (failed == 0) {
                            env.BUILD_NEXT_PROFILE = nextProfile
                            echo "✅ ${currentProfile} tests pass → will build ${nextProfile} next"
                            echo "   Review build-test-report.md for risk assessment"
                        } else {
                            env.BUILD_NEXT_PROFILE = 'none'
                            echo "❌ ${currentProfile} stopping cascade - ${failed} test failures"
                            echo "   Fix tests before cascading to ${nextProfile}"
                        }
                    } else {
                        env.BUILD_NEXT_PROFILE = 'none'
                        echo "🏁 ${currentProfile} is the final profile"
                    }
                }
            }
        }
        
        stage('Version & Tag Build') {
            steps {
                script {
                    // Extract version from package
                    def version = sh(
                        script: """
                            cd ${PACKAGE_DIR}
                            python -c "import tomllib; print(tomllib.load(open('pyproject.toml', 'rb'))['project']['version'])" 2>/dev/null || \
                            grep 'version' pyproject.toml | head -1 | cut -d'"' -f2
                        """,
                        returnStdout: true
                    ).trim()
                    
                    env.PACKAGE_VERSION = version
                    
                    // Add profile suffix for non-stable builds
                    def profileSuffix = env.BUILD_PROFILE == 'stable' ? '' : "+${env.BUILD_PROFILE}"
                    env.BUILD_TAG = "${version}${profileSuffix}.build${BUILD_NUMBER}"
                    
                    // Also set wheel filename for reference
                    env.WHEEL_FILE = "fluid_build-${version}-py3-none-any.whl"
                    
                    env.GIT_COMMIT_SHORT = sh(
                        script: 'git rev-parse --short HEAD',
                        returnStdout: true
                    ).trim()
                    
                    echo "Package Version: ${PACKAGE_VERSION}"
                    echo "Build Profile: ${BUILD_PROFILE}"
                    echo "Build Tag: ${BUILD_TAG}"
                    echo "Git Commit: ${GIT_COMMIT_SHORT}"
                    echo "Providers: ${env.PROVIDERS}"
                    echo "Commands: ${env.COMMAND_COUNT}"
                }
            }
        }
        
        stage('Publish to PyPI') {
            steps {
                echo "� Multi-Profile PyPI Publishing (Alpha + Beta + Conditional Stable)"
                echo "Publishing to private PyPI server: ${PYPI_URL}"
                withCredentials([usernamePassword(credentialsId: 'pypi-server-credentials', 
                                                   usernameVariable: 'PYPI_USER', 
                                                   passwordVariable: 'PYPI_PASS')]) {
                    sh '''
                        cd ${PACKAGE_DIR}
                        . .venv/bin/activate
                        
                        # Export credentials for publish script
                        export PYPI_USER="${PYPI_USER}"
                        export PYPI_PASS="${PYPI_PASS}"
                        
                        # Run multi-profile publishing script
                        chmod +x fluid_build/publish-to-pypi.sh
                        
                        echo "════════════════════════════════════════════════════════"
                        echo "  Publishing All Build Profiles"
                        echo "════════════════════════════════════════════════════════"
                        
                        cd fluid_build
                        ./publish-to-pypi.sh all
                        
                        echo ""
                        echo "✅ All profiles published to ${PYPI_URL}"
                    '''
                }
            }
        }
        
        stage('Build Docker Images') {
            steps {
                echo "🐳 Building Docker images for all build profiles"
                withCredentials([usernamePassword(credentialsId: 'pypi-server-credentials', 
                                                   usernameVariable: 'PYPI_USER', 
                                                   passwordVariable: 'PYPI_PASS')]) {
                    sh '''
                        cd ${PACKAGE_DIR}
                        
                        echo "════════════════════════════════════════════════════════"
                        echo "  Building FLUID CLI Docker Images"
                        echo "════════════════════════════════════════════════════════"
                        
                        # Make build script executable
                        chmod +x build-docker-image.sh
                        
                        # Read version from saved file (created in Build Package stage)
                        # This is necessary because PyPI publishing overwrites dist/ directory
                        if [ ! -f built-version.txt ]; then
                            echo "ERROR: built-version.txt not found!"
                            echo "This file should be created in the Build Package stage"
                            exit 1
                        fi
                        
                        BASE_VERSION=$(cat built-version.txt)
                        BUILD_NUM=${BUILD_NUMBER}
                        
                        echo "📦 Using version from Build Package stage: ${BASE_VERSION}"
                        echo "   (Read from built-version.txt)"
                        
                        # Build Docker images with explicit versions for each profile
                        # Extract core version (strip any dev/alpha/beta suffix for base)
                        CORE_VERSION=$(echo "${BASE_VERSION}" | sed 's/\\.dev[0-9]*//;s/a[0-9]*//;s/b[0-9]*//')
                        
                        echo ""
                        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                        echo "  Building Docker image for profile: experimental"
                        echo "  Version: ${CORE_VERSION}.dev${BUILD_NUM}"
                        echo "  Registry: ${DOCKER_REGISTRY}"
                        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                        ./build-docker-image.sh \
                            --profile experimental \
                            --version "${CORE_VERSION}.dev${BUILD_NUM}" \
                            --registry ${DOCKER_REGISTRY} \
                            --pypi-url ${PYPI_SIMPLE_URL} \
                            --pypi-user "${PYPI_USER}" \
                            --pypi-pass "${PYPI_PASS}" \
                            --no-cache
                        
                        # Also tag as experimental-latest
                        docker tag ${DOCKER_IMAGE}:${CORE_VERSION}.dev${BUILD_NUM} \
                                   ${DOCKER_IMAGE}:experimental-latest
                        docker push ${DOCKER_IMAGE}:experimental-latest
                        
                        echo ""
                        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                        echo "  Building Docker image for profile: alpha"
                        echo "  Version: ${CORE_VERSION}a${BUILD_NUM}"
                        echo "  Registry: ${DOCKER_REGISTRY}"
                        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                        ./build-docker-image.sh \
                            --profile alpha \
                            --version "${CORE_VERSION}a${BUILD_NUM}" \
                            --registry ${DOCKER_REGISTRY} \
                            --pypi-url ${PYPI_SIMPLE_URL} \
                            --pypi-user "${PYPI_USER}" \
                            --pypi-pass "${PYPI_PASS}" \
                            --no-cache
                        
                        docker tag ${DOCKER_IMAGE}:${CORE_VERSION}a${BUILD_NUM} \
                                   ${DOCKER_IMAGE}:alpha-latest
                        docker push ${DOCKER_IMAGE}:alpha-latest
                        
                        echo ""
                        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                        echo "  Building Docker image for profile: beta"
                        echo "  Version: ${CORE_VERSION}b${BUILD_NUM}"
                        echo "  Registry: ${DOCKER_REGISTRY}"
                        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                        ./build-docker-image.sh \
                            --profile beta \
                            --version "${CORE_VERSION}b${BUILD_NUM}" \
                            --registry ${DOCKER_REGISTRY} \
                            --pypi-url ${PYPI_SIMPLE_URL} \
                            --pypi-user "${PYPI_USER}" \
                            --pypi-pass "${PYPI_PASS}" \
                            --no-cache
                        
                        docker tag ${DOCKER_IMAGE}:${CORE_VERSION}b${BUILD_NUM} \
                                   ${DOCKER_IMAGE}:beta-latest
                        docker push ${DOCKER_IMAGE}:beta-latest
                        
                        # ═══════════════════════════════════════════════════════════════
                        # STABLE BUILD - MATURITY GATE
                        # ═══════════════════════════════════════════════════════════════
                        # Stable builds require explicit approval via ALLOW_STABLE_BUILD
                        # This prevents accidentally publishing immature code as stable
                        # ═══════════════════════════════════════════════════════════════
                        
                        if [ "${ALLOW_STABLE_BUILD}" = "true" ]; then
                            echo ""
                            echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                            echo "  Building Docker image for profile: stable"
                            echo "  Version: ${CORE_VERSION}"
                            echo "  🔒 STABLE BUILD APPROVED"
                            echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                            ./build-docker-image.sh \
                                --profile stable \
                                --version "${CORE_VERSION}" \
                                --registry ${DOCKER_REGISTRY} \
                                --pypi-url ${PYPI_SIMPLE_URL} \
                                --pypi-user "${PYPI_USER}" \
                                --pypi-pass "${PYPI_PASS}" \
                                --no-cache
                            
                            docker tag ${DOCKER_IMAGE}:${CORE_VERSION} \
                                       ${DOCKER_IMAGE}:stable-latest
                            docker push ${DOCKER_IMAGE}:stable-latest
                            docker tag ${DOCKER_IMAGE}:${CORE_VERSION} \
                                       ${DOCKER_IMAGE}:latest
                            docker push ${DOCKER_IMAGE}:latest
                            
                            echo "✅ Stable build completed and published"
                        else
                            echo ""
                            echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                            echo "  ⚠️  SKIPPING STABLE BUILD"
                            echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                            echo ""
                            echo "Stable builds are DISABLED by default to prevent publishing"
                            echo "immature code. The codebase is not yet ready for stable release."
                            echo ""
                            echo "To build stable when ready:"
                            echo "  1. Ensure test coverage meets stable criteria"
                            echo "  2. Ensure all providers meet quality standards"
                            echo "  3. Run build with: ALLOW_STABLE_BUILD=true"
                            echo ""
                            echo "Current maturity status:"
                            echo "  ✅ Experimental: Full feature set (kitchen sink)"
                            echo "  ✅ Alpha: Bleeding edge features"
                            echo "  ✅ Beta: Feature complete preview"
                            echo "  ⏸️  Stable: BLOCKED - not yet production ready"
                            echo ""
                            echo "Continuing with experimental/alpha/beta only..."
                            echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                        fi
                        
                        echo ""
                        echo "════════════════════════════════════════════════════════"
                        echo "  Docker Build Summary"
                        echo "════════════════════════════════════════════════════════"
                        
                        # List all built images
                        docker images ${DOCKER_IMAGE} --format "table {{.Repository}}:{{.Tag}}\\t{{.Size}}\\t{{.CreatedAt}}"
                        
                        echo ""
                        echo "✅ All Docker images built and pushed to ${DOCKER_REGISTRY}"
                    '''
                }
            }
        }
        
        // ─────────────────────────────────────────────────────────────────────
        // OPTIONAL: Upload to a separate git-based artifact repository.
        // This stores .whl files + metadata JSON in a dedicated git repo
        // for easy distribution (git clone → pip install).
        //
        // This stage is NOT required — PyPI + Docker + Jenkins archive
        // already have the build artifacts. This is an extra convenience.
        //
        // To enable, you need:
        //   1. A bare git repo on your server:
        //        ssh <user>@<host> "git init --bare /path/to/fluid-cli-builds.git"
        //   2. A Jenkins SSH credential (ID: 'khyana-synology-git-ssh')
        //      containing the private key for the NAS_SSH_USER
        //   3. The ARTIFACT_REPO env var above points to that repo
        //
        // If the repo doesn't exist or credentials are missing, this stage
        // will warn and continue — it won't fail your build.
        // ─────────────────────────────────────────────────────────────────────
        stage('Upload to Artifact Storage') {
            steps {
                catchError(buildResult: 'SUCCESS', stageResult: 'UNSTABLE', message: 'Artifact upload skipped — see console for details') {
                echo "Uploading to separate artifact repository: ${ARTIFACT_REPO}"
                withCredentials([sshUserPrivateKey(credentialsId: 'khyana-synology-git-ssh', keyFileVariable: 'SSH_KEY')]) {
                    sh """#!/bin/bash
                        set -ex
                        # Configure Git to use SSH key
                        export GIT_SSH_COMMAND="ssh -i \${SSH_KEY} -o StrictHostKeyChecking=no"
                        
                        # Clone artifact repository to temp location
                        TEMP_DIR=\$(mktemp -d)
                        git clone \${ARTIFACT_REPO} \${TEMP_DIR} || {
                            echo ""
                            echo "════════════════════════════════════════════════════════"
                            echo "  ⚠️  ARTIFACT REPOSITORY NOT AVAILABLE"
                            echo "════════════════════════════════════════════════════════"
                            echo ""
                            echo "  Could not clone: \${ARTIFACT_REPO}"
                            echo ""
                            echo "  This is OPTIONAL — your build artifacts are already"
                            echo "  available via PyPI, Docker, and Jenkins archive."
                            echo ""
                            echo "  To set up the artifact repo (one-time):"
                            echo "    ssh \${NAS_SSH_USER}@\${NAS_HOST} \\\\"
                            echo "      \\"git init --bare /volume1/git-server/fluid-cli-builds.git\\""
                            echo ""
                            echo "  Also ensure Jenkins credential 'khyana-synology-git-ssh'"
                            echo "  contains a valid SSH private key for \${NAS_SSH_USER}@\${NAS_HOST}"
                            echo "════════════════════════════════════════════════════════"
                            echo ""
                            rm -rf \${TEMP_DIR}
                            exit 1
                        }
                        
                        cd \${TEMP_DIR}
                        
                        # Ensure builds directory exists
                        mkdir -p \${ARTIFACT_DIR}
                        
                        # Copy wheel to artifact directory with build metadata
                        cp \${WORKSPACE}/\${PACKAGE_DIR}/dist/*.whl \${ARTIFACT_DIR}/
                        
                        # Get wheel filename safely
                        WHEEL_FILE=\$(ls \${WORKSPACE}/\${PACKAGE_DIR}/dist/*.whl | xargs -n1 basename)
                        
                        # Determine release type based on build profile
                        if [ "\${BUILD_PROFILE}" = "stable" ]; then
                            RELEASE_TYPE="production"
                        elif [ "\${BUILD_PROFILE}" = "beta" ]; then
                            RELEASE_TYPE="public-beta"
                        else
                            RELEASE_TYPE="development"
                        fi
                    
                    # Create build metadata file with feature release info
                    cat > \${ARTIFACT_DIR}/\${PACKAGE_NAME}-\${BUILD_TAG}.json <<EOF_JSON
{
  "version": "\${PACKAGE_VERSION}",
  "build_number": "\${BUILD_NUMBER}",
  "build_tag": "\${BUILD_TAG}",
  "build_profile": "\${BUILD_PROFILE}",
  "git_commit": "\${GIT_COMMIT_SHORT}",
  "git_branch": "\${GIT_BRANCH}",
  "build_date": "\$(date -Iseconds)",
  "jenkins_url": "\${BUILD_URL}",
  "built_by": "\${BUILD_USER:-jenkins}",
  "wheel_file": "\${WHEEL_FILE}",
  "features": {
    "provider_count": \${PROVIDER_COUNT:-0},
    "command_count": \${COMMAND_COUNT:-0},
    "providers": "\${PROVIDERS}"
  },
  "release_type": "\${RELEASE_TYPE}"
}
EOF_JSON
                    
                    # Create/update latest symlink (for easy access)
                    ln -sf \${WHEEL_FILE} \${ARTIFACT_DIR}/fluid-forge-latest.whl
                    
                    # Update index file
                    cat > \${ARTIFACT_DIR}/index.html <<'EOF_HTML'
<!DOCTYPE html>
<html>
<head>
    <title>FLUID CLI Builds</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 40px; }
        table { border-collapse: collapse; width: 100%; }
        th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
        th { background-color: #4CAF50; color: white; }
        tr:hover { background-color: #f5f5f5; }
        .latest { background-color: #ffeb3b; }
        .stable { background-color: #4CAF50; color: white; }
        .beta { background-color: #FF9800; color: white; }
        .alpha { background-color: #9E9E9E; color: white; }
    </style>
</head>
<body>
    <h1>FLUID CLI Build Artifacts</h1>
    <h2>Available Builds:</h2>
EOF_HTML
                    
                    # Add file listing to index
                    echo "<table><tr><th>File</th><th>Size</th><th>Date</th></tr>" >> \${ARTIFACT_DIR}/index.html
                    ls -lht \${ARTIFACT_DIR}/*.whl | awk '{print "<tr><td>"FILENAME"</td><td>"\$5"</td><td>"\$6" "\$7" "\$8"</td></tr>"}' FILENAME="\${WHEEL_FILE}" >> \${ARTIFACT_DIR}/index.html
                    echo "</table></body></html>" >> \${ARTIFACT_DIR}/index.html
                    
                    # Commit and push to artifact repository
                    git config user.email "jenkins@fluid-mono"
                    git config user.name "Jenkins CI"
                    git add \${ARTIFACT_DIR}/
                    git commit -m "Build \${BUILD_TAG} from commit \${GIT_COMMIT_SHORT}" || true
                    
                    # Push using the same SSH config
                    git push origin master
                    
                    # Cleanup
                    cd \${WORKSPACE}
                    rm -rf \${TEMP_DIR}
                    
                    echo "✅ Artifact uploaded successfully!"
                    echo "📦 Download: git clone \${ARTIFACT_REPO}"
                    """
                }
                } // catchError
            }
        }
        
        stage('Generate Build Report') {
            steps {
                echo "📝 Generating comprehensive AI/CI build report..."
                sh """#!/bin/bash
                    set -e
                    cd ${PACKAGE_DIR}
                    . .venv/bin/activate
                    
                    # Make report generator executable
                    chmod +x scripts/generate_build_report.py
                    
                    # Generate report (will create build-reports/ directory)
                    python scripts/generate_build_report.py
                    
                    echo ""
                    echo "=== Build Report Generated ==="
                    ls -lh build-reports/ || echo "No reports directory"
                    
                    if [ -f "build-reports/build-report.md" ]; then
                        echo ""
                        echo "=== Quick Summary (First 50 lines) ==="
                        head -50 build-reports/build-report.md
                    fi
                """
                
                // Archive the reports as Jenkins artifacts
                script {
                    try {
                        archiveArtifacts artifacts: "build-reports/**/*", fingerprint: true, allowEmptyArchive: true
                    } catch (Exception e) {
                        echo "⚠️  No build reports to archive"
                    }
                }
            }
        }
        
        stage('Archive Build Logs to Git') {
            steps {
                catchError(buildResult: 'SUCCESS', stageResult: 'UNSTABLE', message: 'Build log archiving skipped') {
                echo "📚 Archiving comprehensive build logs to git repository..."
                sh """#!/bin/bash
                    set -e
                    cd ${PACKAGE_DIR}
                    
                    # Create build log directory with timestamp
                    LOG_DIR="daily_context_store/build-logs/\${BUILD_TAG}"
                    mkdir -p "\${LOG_DIR}"
                    
                    # Copy all reports
                    if [ -d "build-reports" ]; then
                        cp -r build-reports/* "\${LOG_DIR}/" 2>/dev/null || true
                    fi
                    
                    # Copy test artifacts
                    [ -f "coverage.json" ] && cp coverage.json "\${LOG_DIR}/" || true
                    [ -f "test-report.json" ] && cp test-report.json "\${LOG_DIR}/" || true
                    [ -f ".coverage" ] && cp .coverage "\${LOG_DIR}/" || true
                    
                    # Create build metadata
                    cat > "\${LOG_DIR}/build-meta.json" <<META_EOF
{
  "build_number": "\${BUILD_NUMBER}",
  "build_tag": "\${BUILD_TAG}",
  "profile": "\${BUILD_PROFILE}",
  "branch": "\${GIT_BRANCH}",
  "commit": "\${GIT_COMMIT}",
  "commit_short": "\${GIT_COMMIT_SHORT}",
  "timestamp": "\$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "url": "\${BUILD_URL}",
  "provider_count": "\${PROVIDER_COUNT}",
  "command_count": "\${COMMAND_COUNT}",
  "coverage": "\${OVERALL_COVERAGE}",
  "tests_passed": "\${TESTS_PASSED}",
  "tests_failed": "\${TESTS_FAILED}"
}
META_EOF
                    
                    echo ""
                    echo "=== Build Logs Directory ==="
                    ls -lah "\${LOG_DIR}"
                    
                    # Commit logs to git for persistence
                    git config --global user.email "jenkins@fluid-ci"
                    git config --global user.name "Jenkins CI"
                    git add daily_context_store/build-logs/
                    
                    if git commit -m "ci: archive build logs for ${BUILD_TAG} [skip ci]"; then
                        echo "✅ Committed build logs"
                        # Try to push, but don't fail if credentials missing
                        if git push origin ${GIT_BRANCH}; then
                            echo "✅ Pushed build logs to origin/${GIT_BRANCH}"
                        else
                            echo "⚠️  Could not push logs (may need credentials configured)"
                            echo "Logs are committed locally and will be pushed with next manual push"
                        fi
                    else
                        echo "ℹ️  No new logs to commit"
                    fi
                    
                    echo ""
                    echo "✅ Build logs archived to: daily_context_store/build-logs/\${BUILD_TAG}"
                    echo "📁 Access via: git log --all --name-only | grep build-logs"
                """
                } // catchError
            }
        }
        
        stage('Archive Build') {
            steps {
                echo "Archiving build artifacts in Jenkins"
                archiveArtifacts artifacts: "dist/*.whl,dist/*.tar.gz", fingerprint: true
                
                // Create download instructions
                sh """
                    cat > ${WORKSPACE}/INSTALL_INSTRUCTIONS.txt << 'EOF'
=== FLUID CLI Installation Instructions ===

Build: ${BUILD_TAG}
Commit: ${GIT_COMMIT_SHORT}
Date: \$(date)

🎯 OPTION 1: Install from Private PyPI (Recommended)
----------------------------------------------------
pip install --index-url ${PYPI_SIMPLE_URL} fluid-forge

# Or set as default in pip.conf:
# [global]
# index-url = ${PYPI_SIMPLE_URL}
# trusted-host = ${NAS_HOST}

🐳 OPTION 2: Use Docker Image (Production Ready)
-------------------------------------------------
# Pull specific profile
docker pull ${DOCKER_IMAGE}:stable
docker pull ${DOCKER_IMAGE}:beta
docker pull ${DOCKER_IMAGE}:alpha
docker pull ${DOCKER_IMAGE}:experimental

# Run commands
docker run --rm ${DOCKER_IMAGE}:stable --version
docker run --rm -v \$(pwd):/workspace ${DOCKER_IMAGE}:stable \\
    validate /workspace/contract.fluid.yaml

# Use in Jenkins pipeline
docker {
    image '${DOCKER_IMAGE}:stable'
    args '-v \$WORKSPACE:/workspace'
}

📦 OPTION 3: Install from Artifact Repository
----------------------------------------------
git clone ${ARTIFACT_REPO}
pip install fluid-cli-builds/${ARTIFACT_DIR}/fluid-forge-latest.whl

📥 OPTION 4: Install from Jenkins Archive
------------------------------------------
1. Download wheel from: ${BUILD_URL}artifact/${PACKAGE_DIR}/dist/
2. Install: pip install fluid_build-*.whl

🛠️ OPTION 5: Using Installation Script
---------------------------------------
# From the source repository
cd fluid_forge/fluid-forge-cli
./install-fluid-cli.sh

🏷️ AVAILABLE PROFILES:
----------------------
  experimental  - Kitchen sink (ALL commands & providers)
  alpha         - Bleeding edge features
  beta          - Feature complete preview  
  stable        - Production ready

EOF
                    cat ${WORKSPACE}/INSTALL_INSTRUCTIONS.txt
                """
                
                archiveArtifacts artifacts: "INSTALL_INSTRUCTIONS.txt", fingerprint: true
            }
        }
        
        stage('Build Next Profile') {
            when {
                expression { 
                    // DISABLED - Multi-profile publishing now happens in one stage
                    return false
                }
            }
            steps {
                echo "⚠️  Profile cascading disabled - using multi-profile publishing instead"
            }
        }
        
        stage('📦 Delivery Summary') {
            steps {
                sh '''#!/bin/bash
                    set +e  # Don't fail on errors, we want to show what we have
                    
                    cd ${PACKAGE_DIR}
                    
                    # Calculate build duration
                    BUILD_END=$(date +%s)
                    
                    # Gather stats
                    COMMIT_SHORT=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
                    COMMIT_MSG=$(git log -1 --pretty=%s 2>/dev/null || echo "unknown")
                    COMMIT_AUTHOR=$(git log -1 --pretty=%an 2>/dev/null || echo "unknown")
                    
                    PYPI_DELIVERED=0
                    PYPI_SKIPPED=0
                    DOCKER_DELIVERED=0
                    DOCKER_SKIPPED=0
                    
                    for profile in experimental alpha beta stable; do
                        COUNT=$(ls -1 fluid_build/pypi/${profile}/*.whl 2>/dev/null | wc -l)
                        if [ "$COUNT" -gt 0 ]; then
                            PYPI_DELIVERED=$((PYPI_DELIVERED + 1))
                        else
                            PYPI_SKIPPED=$((PYPI_SKIPPED + 1))
                        fi
                    done
                    
                    if command -v docker > /dev/null 2>&1; then
                        for tag in experimental-latest alpha-latest beta-latest stable-latest; do
                            if docker images ${DOCKER_IMAGE}:${tag} --format "{{.Repository}}" 2>/dev/null | grep -q fluid-forge-cli; then
                                DOCKER_DELIVERED=$((DOCKER_DELIVERED + 1))
                            else
                                DOCKER_SKIPPED=$((DOCKER_SKIPPED + 1))
                            fi
                        done
                    fi
                    
                    TOTAL_DELIVERED=$((PYPI_DELIVERED + DOCKER_DELIVERED))
                    TOTAL_ARTIFACTS=$((TOTAL_DELIVERED + PYPI_SKIPPED + DOCKER_SKIPPED))
                    
                    echo ""
                    echo ""
                    echo "╔═════════════════════════════════════════════════════════════════════════════════╗"
                    echo "║                                                                                 ║"
                    echo "║     ███████╗██╗     ██╗   ██╗██╗██████╗     ███████╗ ██████╗ ██████╗  ██████╗ ███████╗  ║"
                    echo "║     ██╔════╝██║     ██║   ██║██║██╔══██╗    ██╔════╝██╔═══██╗██╔══██╗██╔════╝ ██╔════╝  ║"
                    echo "║     █████╗  ██║     ██║   ██║██║██║  ██║    █████╗  ██║   ██║██████╔╝██║  ███╗█████╗    ║"
                    echo "║     ██╔══╝  ██║     ██║   ██║██║██║  ██║    ██╔══╝  ██║   ██║██╔══██╗██║   ██║██╔══╝    ║"
                    echo "║     ██║     ███████╗╚██████╔╝██║██████╔╝    ██║     ╚██████╔╝██║  ██║╚██████╔╝███████╗  ║"
                    echo "║     ╚═╝     ╚══════╝ ╚═════╝ ╚═╝╚═════╝     ╚═╝      ╚═════╝ ╚═╝  ╚═╝ ╚═════╝ ╚══════╝  ║"
                    echo "║                                                                                 ║"
                    echo "║                        BUILD PIPELINE REPORT                                    ║"
                    echo "║                                                                                 ║"
                    echo "╚═════════════════════════════════════════════════════════════════════════════════╝"
                    echo ""
                    
                    echo "┌─────────────────────────────────────────────────────────────────────┐"
                    echo "│  📊 BUILD OVERVIEW                                                  │"
                    echo "├─────────────────────────────────────────────────────────────────────┤"
                    echo "│  Build Tag:      ${BUILD_TAG}"
                    echo "│  Profile:        ${BUILD_PROFILE}"
                    echo "│  Branch:         ${BRANCH_NAME:-unknown}"
                    echo "│  Commit:         ${COMMIT_SHORT} — ${COMMIT_MSG}"
                    echo "│  Author:         ${COMMIT_AUTHOR}"
                    echo "│  Build #:        ${BUILD_NUMBER}"
                    echo "│  Timestamp:      $(date '+%Y-%m-%d %H:%M:%S %Z')"
                    echo "│  Jenkins:        ${BUILD_URL}"
                    echo "└─────────────────────────────────────────────────────────────────────┘"
                    echo ""
                    
                    echo "┌─────────────────────────────────────────────────────────────────────┐"
                    echo "│  🧪 QUALITY & TESTING                                               │"
                    echo "├─────────────────────────────────────────────────────────────────────┤"
                    
                    TEST_TOTAL=${TESTS_TOTAL:-0}
                    TEST_PASSED=${TESTS_PASSED:-0}
                    TEST_FAILED=${TESTS_FAILED:-0}
                    COVERAGE=${OVERALL_COVERAGE:-0}
                    
                    if [ "$TEST_TOTAL" -gt 0 ]; then
                        PASS_RATE=$(echo "scale=1; $TEST_PASSED * 100 / $TEST_TOTAL" | bc 2>/dev/null || echo "?")
                        echo "│  Tests Run:      ${TEST_TOTAL}"
                        echo "│  Tests Passed:   ${TEST_PASSED}  ✅"
                        if [ "$TEST_FAILED" -gt 0 ]; then
                            echo "│  Tests Failed:   ${TEST_FAILED}  ❌"
                        else
                            echo "│  Tests Failed:   0"
                        fi
                        echo "│  Pass Rate:      ${PASS_RATE}%"
                        echo "│  Coverage:       ${COVERAGE}%"
                    else
                        echo "│  Tests:          No tests executed"
                    fi
                    
                    echo "│  Providers:      ${PROVIDER_COUNT:-0} (${PROVIDERS:-none})"
                    echo "│  Commands:       ${COMMAND_COUNT:-0}"
                    echo "└─────────────────────────────────────────────────────────────────────┘"
                    echo ""
                    
                    echo "┌─────────────────────────────────────────────────────────────────────┐"
                    echo "│  📦 PYPI PACKAGES                        Server: ${PYPI_URL}"
                    echo "├─────────────────────────────────────────────────────────────────────┤"
                    
                    for profile in experimental alpha beta stable; do
                        LATEST=$(ls -1t fluid_build/pypi/${profile}/*.whl 2>/dev/null | head -1)
                        if [ -n "$LATEST" ]; then
                            WHEEL_NAME=$(basename "$LATEST")
                            WHEEL_SIZE=$(du -h "$LATEST" | cut -f1)
                            printf "│  ✅ %-15s %s (%s)\n" "${profile}" "${WHEEL_NAME}" "${WHEEL_SIZE}"
                        else
                            printf "│  ⏭️  %-15s skipped\n" "${profile}"
                        fi
                    done
                    
                    echo "│"
                    echo "│  Install:  pip install --index-url ${PYPI_SIMPLE_URL} fluid-forge"
                    echo "└─────────────────────────────────────────────────────────────────────┘"
                    echo ""
                    
                    echo "┌─────────────────────────────────────────────────────────────────────┐"
                    echo "│  🐳 DOCKER IMAGES                        Registry: ${DOCKER_REGISTRY}"
                    echo "├─────────────────────────────────────────────────────────────────────┤"
                    
                    if command -v docker > /dev/null 2>&1; then
                        for tag in experimental-latest alpha-latest beta-latest stable-latest; do
                            PROFILE_NAME=$(echo "$tag" | sed 's/-latest//')
                            if docker images ${DOCKER_IMAGE}:${tag} --format "{{.Size}}" 2>/dev/null | head -1 | grep -q .; then
                                IMG_SIZE=$(docker images ${DOCKER_IMAGE}:${tag} --format "{{.Size}}" | head -1)
                                printf "│  ✅ %-15s %s:%s (%s)\n" "${PROFILE_NAME}" "${DOCKER_IMAGE}" "${tag}" "${IMG_SIZE}"
                            else
                                printf "│  ⏭️  %-15s skipped\n" "${PROFILE_NAME}"
                            fi
                        done
                        echo "│"
                        echo "│  Pull:    docker pull ${DOCKER_IMAGE}:<profile>"
                        echo "│  Run:     docker run --rm ${DOCKER_IMAGE}:experimental --help"
                    else
                        echo "│  ⚠️  Docker not available on this build agent"
                    fi
                    
                    echo "└─────────────────────────────────────────────────────────────────────┘"
                    echo ""
                    
                    echo "┌─────────────────────────────────────────────────────────────────────┐"
                    echo "│  📋 ARTIFACTS & REPORTS                                             │"
                    echo "├─────────────────────────────────────────────────────────────────────┤"
                    
                    # Wheel artifacts
                    if [ -d "dist" ]; then
                        for whl in dist/*.whl; do
                            [ -f "$whl" ] && printf "│  📦 %s (%s)\n" "$(basename $whl)" "$(du -h $whl | cut -f1)"
                        done
                    fi
                    
                    # Build reports
                    if [ -d "build-reports" ]; then
                        for report in build-reports/*; do
                            [ -f "$report" ] && printf "│  📄 %s\n" "$(basename $report)"
                        done
                    fi
                    
                    echo "│"
                    echo "│  Git Artifacts:  ${ARTIFACT_REPO}"
                    echo "│  Jenkins:        ${BUILD_URL}artifact/"
                    echo "└─────────────────────────────────────────────────────────────────────┘"
                    echo ""
                    
                    echo "┌─────────────────────────────────────────────────────────────────────┐"
                    echo "│  ✅ DELIVERY VERIFICATION                                           │"
                    echo "├─────────────────────────────────────────────────────────────────────┤"
                    
                    ERRORS=0
                    
                    for profile in experimental alpha beta stable; do
                        if [ -d "fluid_build/pypi/${profile}" ]; then
                            COUNT=$(ls -1 fluid_build/pypi/${profile}/*.whl 2>/dev/null | wc -l)
                            if [ "$COUNT" -gt 0 ]; then
                                printf "│  ✅ PyPI %-14s DELIVERED\n" "${profile}"
                            else
                                printf "│  ⏭️  PyPI %-14s skipped\n" "${profile}"
                            fi
                        fi
                    done
                    
                    if command -v docker > /dev/null 2>&1; then
                        for profile in experimental alpha beta stable; do
                            if docker images ${DOCKER_IMAGE}:${profile}-latest --format "{{.Repository}}" 2>/dev/null | grep -q fluid-forge-cli; then
                                printf "│  ✅ Docker %-12s DELIVERED\n" "${profile}"
                            else
                                printf "│  ⏭️  Docker %-12s skipped\n" "${profile}"
                            fi
                        done
                    fi
                    
                    echo "│"
                    printf "│  Total Delivered: %d / %d\n" "$TOTAL_DELIVERED" "$TOTAL_ARTIFACTS"
                    echo "└─────────────────────────────────────────────────────────────────────┘"
                    echo ""
                    
                    if [ "$TOTAL_DELIVERED" -gt 0 ]; then
                        echo "╔═════════════════════════════════════════════════════════════════════════════════╗"
                        echo "║                      ✅  BUILD PIPELINE COMPLETE                                ║"
                        echo "╚═════════════════════════════════════════════════════════════════════════════════╝"
                    else
                        echo "╔═════════════════════════════════════════════════════════════════════════════════╗"
                        echo "║                   ⚠️  BUILD COMPLETE — NO DELIVERABLES                          ║"
                        echo "╚═════════════════════════════════════════════════════════════════════════════════╝"
                    fi
                    
                    echo ""
                    echo "─────────────────────────────────────────────────────────────────────────"
                    echo "  FLUID Forge CLI — Proudly developed by dustlabs.co.za"
                    echo "  Open source under the Apache 2.0 License"
                    echo "  https://dustlabs.co.za"
                    echo "─────────────────────────────────────────────────────────────────────────"
                    echo ""
                    
                    exit 0  # Don't fail the build on summary errors
                '''
            }
        }
    }
    
    parameters {
        // Infrastructure config - fill these in on first run via "Build with Parameters"
        // Jenkins remembers the last used values for subsequent builds
        string(name: 'NAS_HOST', defaultValue: '192.168.178.121', description: '🖥️ NAS IP or hostname (e.g. 192.168.1.100)')
        string(name: 'NAS_SSH_USER', defaultValue: 'khyana_ai', description: '👤 SSH user on the NAS for git operations')
        string(name: 'PYPI_PORT', defaultValue: '8080', description: '📦 Port for the private PyPI server on the NAS')
        string(name: 'DOCKER_REGISTRY', defaultValue: 'localhost:5000', description: '🐳 Docker registry host:port for pushing built images')
        
        // Build config
        string(name: 'OVERRIDE_PROFILE', defaultValue: '', description: 'Force a specific profile (alpha/beta/stable)')
        string(name: 'PARENT_BUILD', defaultValue: '', description: 'Parent build tag if this is a promoted build')
        booleanParam(name: 'ALLOW_STABLE_BUILD', defaultValue: false, description: '🔒 Allow building STABLE profile (requires maturity criteria)')
    }
    
    post {
        success {
            script {
                // Safe access to env vars (may not be available in post context)
                def buildProfile = env.BUILD_PROFILE ?: 'unknown'
                def profileEmoji = buildProfile == 'stable' ? '🚀' : 
                                  buildProfile == 'beta' ? '⚠️' : '🔧'
                echo "${profileEmoji} Build successful! Package ${BUILD_TAG} is ready."
                echo "Profile: ${buildProfile}"
                
                // Try to access feature counts if available
                try {
                    echo "Features: ${env.PROVIDER_COUNT} providers, ${env.COMMAND_COUNT} commands"
                    echo "Providers: ${env.PROVIDERS}"
                } catch (Exception e) {
                    echo "Feature info not available"
                }
            }
            
            // Optional: Notify team (Slack, email, etc.)
            // slackSend(color: 'good', message: "${profileEmoji} FLUID CLI ${BUILD_TAG} (${BUILD_PROFILE}) built successfully!\nFeatures: ${PROVIDER_COUNT} providers, ${COMMAND_COUNT} commands")
        }
        
        failure {
            script {
                // Don't report failure on first-run parameter registration
                if (!params.NAS_HOST?.trim()) {
                    return
                }
            }
            echo "❌ Build failed!"
            echo "Branch: ${env.BRANCH_NAME}"
            
            // Optional: Notify team
            // slackSend(color: 'danger', message: "❌ FLUID CLI build failed!\nBranch: ${BRANCH_NAME}\nCheck ${BUILD_URL}")
        }
        
        always {
            script {
                // If this was a first-run parameter registration, reset to NOT_BUILT (grey)
                if (!params.NAS_HOST?.trim()) {
                    currentBuild.result = 'NOT_BUILT'
                    return
                }
                // Cleanup - only if we have a workspace
                try {
                    sh '''
                        cd ${PACKAGE_DIR}
                        rm -rf .venv build *.egg-info || true
                    '''
                } catch (Exception e) {
                    echo "Cleanup skipped (no workspace context)"
                }
            }
        }
    }
}
