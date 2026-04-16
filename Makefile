# Makefile — developer + CI ergonomics for FLUID Build

# Cross-platform detection with WSL support
UNAME_S := $(shell uname -s 2>/dev/null || echo Windows)
WSL_CHECK := $(shell uname -r 2>/dev/null | grep -i microsoft 2>/dev/null || echo "")

# Determine platform and set variables accordingly
ifeq ($(UNAME_S),Windows_NT)
	# Native Windows (Git Bash or CMD)
	PYTHON ?= python
	PIP ?= pip
	VENV ?= .venv
	ACTIVATE = $(VENV)/Scripts/activate
	PYTHON_EXEC = $(VENV)/Scripts/python.exe
	PIP_EXEC = $(VENV)/Scripts/pip.exe
	PLATFORM = Windows
else ifneq ($(WSL_CHECK),)
	# WSL (Windows Subsystem for Linux)
	PYTHON ?= python3
	PIP ?= pip3
	VENV ?= .venv
	ACTIVATE = . $(VENV)/bin/activate
	PYTHON_EXEC = $(VENV)/bin/python
	PIP_EXEC = $(VENV)/bin/pip
	PLATFORM = WSL
else ifeq ($(UNAME_S),Darwin)
	# macOS
	PYTHON ?= python3
	PIP ?= pip3
	VENV ?= .venv
	ACTIVATE = . $(VENV)/bin/activate
	PYTHON_EXEC = $(VENV)/bin/python
	PIP_EXEC = $(VENV)/bin/pip
	PLATFORM = macOS
else
	# Linux
	PYTHON ?= python3
	PIP ?= pip3
	VENV ?= .venv
	ACTIVATE = . $(VENV)/bin/activate
	PYTHON_EXEC = $(VENV)/bin/python
	PIP_EXEC = $(VENV)/bin/pip
	PLATFORM = Linux
endif

PKG_NAME = data-product-forge

.DEFAULT_GOAL := setup

setup: ## 🚀 One-command setup for first-time users (cross-platform)
	@echo "🚀 Setting up FLUID Build development environment..."
	@echo "Platform: $(PLATFORM)"
	@echo "=================================================="
	@$(MAKE) venv
	@echo "📦 Installing in development mode with all extras..."
	@$(MAKE) install-all
	@echo "🔍 Running health checks..."
	@$(MAKE) doctor
	@echo "✅ Testing basic commands..."
	@$(MAKE) version
	@$(MAKE) providers
	@echo ""
	@echo "🎉 Setup complete! You can now use FLUID Build:"
ifeq ($(PLATFORM),Windows)
	@echo "   $(ACTIVATE) && python -m fluid_build.cli --help"
	@echo "   $(ACTIVATE) && python -m fluid_build.cli version"
	@echo "   $(ACTIVATE) && python -m fluid_build.cli validate examples/customer360/contract.fluid.yaml"
else
	@echo "   source $(VENV)/bin/activate   # Activate the environment"
	@echo "   python -m fluid_build.cli --help"
	@echo "   python -m fluid_build.cli version"
	@echo "   python -m fluid_build.cli validate examples/customer360/contract.fluid.yaml"
endif
	@echo ""

check-platform: ## 🔍 Check platform and show environment info
	@echo "🔍 Platform Detection:"
	@echo "  Platform: $(PLATFORM)"
	@echo "  OS: $(UNAME_S)"
	@echo "  WSL Check: $(WSL_CHECK)"
	@echo "  Python: $(PYTHON)"
	@echo "  Pip: $(PIP)"
	@echo "  Virtual Env: $(VENV)"
	@echo "  Activation: $(ACTIVATE)"
	@echo ""

help: ## Show available targets
	@awk 'BEGIN{FS=" ## "; printf "\n\033[1mTargets\033[0m\n"} /^[a-zA-Z0-9_-]+:.*##/{printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo ""

venv: ## 📦 Create virtual environment (cross-platform)
	@echo "📦 Creating virtual environment..."
	@echo "Platform: $(PLATFORM)"
ifeq ($(PLATFORM),Windows)
	@if not exist "$(VENV)" ($(PYTHON) -m venv $(VENV))
	@echo "✅ Virtual environment created at: $(VENV)"
	@$(ACTIVATE) && $(PIP) --version
else
	@test -d $(VENV) || $(PYTHON) -m venv $(VENV)
	@echo "✅ Virtual environment created at: $(VENV)"
	@# Prefer using the venv's pip executable directly. Some Python installs (Windows
	# Python invoked from WSL) create a venv with a Scripts/ layout instead of bin/.
	@if [ -x "$(PIP_EXEC)" ]; then \
		$(PIP_EXEC) --version; \
	elif [ -x "$(VENV)/Scripts/pip" ]; then \
		"$(VENV)/Scripts/pip" --version; \
	elif [ -x "$(VENV)/Scripts/pip.exe" ]; then \
		"$(VENV)/Scripts/pip.exe" --version; \
	else \
		echo "Could not find pip in venv. Listing venv contents for debugging:"; \
		ls -la $(VENV) || true; \
		exit 1; \
	fi
endif

install: venv ## Install package (non-editable)
	@bash -lc '\
if [ -x "$(VENV)/bin/python" ]; then vp="$(VENV)/bin/python"; \
elif [ -x "$(VENV)/Scripts/python.exe" ]; then vp="$(VENV)/Scripts/python.exe"; \
elif command -v python3 >/dev/null 2>&1; then vp=python3; else vp=python; fi; \
echo "Using: $$vp"; \
$$vp -m pip install --upgrade pip wheel; \
$$vp -m pip install .;'

install-dev: venv ## Install package in editable mode with dev extras
	@bash -lc '\
if [ -x "$(VENV)/bin/python" ]; then vp="$(VENV)/bin/python"; \
elif [ -x "$(VENV)/Scripts/python.exe" ]; then vp="$(VENV)/Scripts/python.exe"; \
elif command -v python3 >/dev/null 2>&1; then vp=python3; else vp=python; fi; \
echo "Using: $$vp"; \
$$vp -m pip install --upgrade pip wheel; \
$$vp -m pip install -e ".[dev]";'

install-all: venv ## Install with all extras (gcp, snowflake, viz, dev)
	@bash -lc '\
if [ -x "$(VENV)/bin/python" ]; then vp="$(VENV)/bin/python"; \
elif [ -x "$(VENV)/Scripts/python.exe" ]; then vp="$(VENV)/Scripts/python.exe"; \
elif command -v python3 >/dev/null 2>&1; then vp=python3; else vp=python; fi; \
echo "Using: $$vp"; \
$$vp -m pip install --upgrade pip wheel; \
$$vp -m pip install -e ".[all]";'

# ---- development workflow ----

dev: install-all ## Alias for install-all (development setup)

clean: ## 🧹 Clean up build artifacts and caches (cross-platform)
ifeq ($(PLATFORM),Windows)
	@if exist "build" rmdir /s /q "build" 2>nul || echo "No build directory"
	@if exist "dist" rmdir /s /q "dist" 2>nul || echo "No dist directory"
	@for /d /r . %%d in (*egg-info) do @if exist "%%d" rmdir /s /q "%%d" 2>nul
	@for /d /r . %%d in (__pycache__) do @if exist "%%d" rmdir /s /q "%%d" 2>nul
	@for /r . %%f in (*.pyc) do @if exist "%%f" del "%%f" 2>nul
else
	@rm -rf build/ dist/ *.egg-info/
	@find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name "*.pyc" -delete 2>/dev/null || true
endif

reset: clean ## 🔄 Reset environment (clean + remove venv)
ifeq ($(PLATFORM),Windows)
	@if exist "$(VENV)" rmdir /s /q "$(VENV)"
else
	@rm -rf $(VENV)
endif
	@echo "🧹 Environment reset. Run 'make setup' to start fresh."


lint: ## Run ruff + black check
	@bash -lc '\
if [ -x "$(VENV)/bin/python" ]; then vp="$(VENV)/bin/python"; \
elif [ -x "$(VENV)/Scripts/python.exe" ]; then vp="$(VENV)/Scripts/python.exe"; \
else vp=$(PYTHON); fi; \
echo "Using: $$vp"; \
$$vp -m ruff check fluid_build || true; \
$$vp -m black --check fluid_build || true;'


fmt: ## Auto-format code
	@bash -lc '\
if [ -x "$(VENV)/bin/python" ]; then vp="$(VENV)/bin/python"; \
elif [ -x "$(VENV)/Scripts/python.exe" ]; then vp="$(VENV)/Scripts/python.exe"; \
else vp=$(PYTHON); fi; \
echo "Using: $$vp"; \
$$vp -m black fluid_build || true; \
$$vp -m ruff check --fix fluid_build || true;'


typecheck: ## Run mypy (if you keep stubs)
	@bash -lc '\
if [ -x "$(VENV)/bin/python" ]; then vp="$(VENV)/bin/python"; \
elif [ -x "$(VENV)/Scripts/python.exe" ]; then vp="$(VENV)/Scripts/python.exe"; \
else vp=$(PYTHON); fi; \
echo "Using: $$vp"; \
$$vp -m mypy fluid_build || true;'


test: ## Run unit tests
	@bash -lc '\
if [ -x "$(VENV)/bin/python" ]; then vp="$(VENV)/bin/python"; \
elif [ -x "$(VENV)/Scripts/python.exe" ]; then vp="$(VENV)/Scripts/python.exe"; \
else vp=$(PYTHON); fi; \
echo "Using: $$vp"; \
$$vp -m pytest -q --maxfail=1 --disable-warnings;'

doctor: ## Run doctor with optional extended workspace diagnostics
	@bash -lc '\
if [ -x "$(VENV)/bin/python" ]; then vp="$(VENV)/bin/python"; \
elif [ -x "$(VENV)/Scripts/python.exe" ]; then vp="$(VENV)/Scripts/python.exe"; \
else vp=$(PYTHON); fi; \
echo "Using: $$vp"; \
FLUID_LOG_LEVEL=INFO $$vp -m fluid_build.cli doctor --extended || true;'

check-action-pins: ## 🔒 Check if pinned GitHub Action SHAs are up to date
	@echo "🔒 Checking pinned action SHAs..."
	@$(PYTHON) scripts/check_pinned_actions.py

# ---- pipx flows ----

pipx-install: ## Install 'fluid' CLI globally via pipx
	@command -v pipx >/dev/null 2>&1 || (echo "pipx not found. Install with: python3 -m pip install --user pipx && python3 -m pipx ensurepath" && exit 1)
	@pipx install .
	@echo "✅ Installed. Try: fluid version && fluid providers"

pipx-inject-gcp: ## Add GCP extras into pipx venv
	@pipx inject fluid "google-cloud-bigquery>=3.0.0" "google-auth>=2.21.0"

pipx-inject-snowflake: ## Add Snowflake extras into pipx venv
	@pipx inject fluid "snowflake-connector-python>=3.5.0"

pipx-inject-viz: ## Add viz extras into pipx venv
	@pipx inject fluid "graphviz>=0.20.1"

pipx-uninstall: ## Remove global CLI
	@pipx uninstall fluid || true

# ---- quick sanity commands ----

demo: ## Run a quick demo workflow (validate -> plan -> apply)
	@echo "🎯 Running FLUID Build demo workflow..."
	@$(MAKE) validate
	@$(MAKE) plan
	@echo "✅ Demo complete! Check runtime/ directory for outputs."

validate: ## Validate example contracts
	@bash -lc '\
if [ -x "$(VENV)/bin/python" ]; then vp="$(VENV)/bin/python"; \
elif [ -x "$(VENV)/Scripts/python.exe" ]; then vp="$(VENV)/Scripts/python.exe"; \
else vp=$(PYTHON); fi; \
echo "Using: $$vp"; \
$$vp -m fluid_build.cli validate examples/customer360/contract.fluid.yaml;'


plan: ## Plan (local)
	@bash -lc '\
if [ -x "$(VENV)/bin/python" ]; then vp="$(VENV)/bin/python"; \
elif [ -x "$(VENV)/Scripts/python.exe" ]; then vp="$(VENV)/Scripts/python.exe"; \
else vp=$(PYTHON); fi; \
echo "Using: $$vp"; \
$$vp -m fluid_build.cli --provider local plan examples/local/high_value_churn/contract.fluid.yaml --out runtime/plan.json;'


apply-local: ## Apply (local provider)
	@bash -lc '\
if [ -x "$(VENV)/bin/python" ]; then vp="$(VENV)/bin/python"; \
elif [ -x "$(VENV)/Scripts/python.exe" ]; then vp="$(VENV)/Scripts/python.exe"; \
else vp=$(PYTHON); fi; \
echo "Using: $$vp"; \
$$vp -m fluid_build.cli --provider local apply examples/local/high_value_churn/contract.fluid.yaml --out runtime/local_apply_report.json;'


providers: ## Show providers
	@bash -lc '\
if [ -x "$(VENV)/bin/python" ]; then vp="$(VENV)/bin/python"; \
elif [ -x "$(VENV)/Scripts/python.exe" ]; then vp="$(VENV)/Scripts/python.exe"; \
else vp=$(PYTHON); fi; \
echo "Using: $$vp"; \
$$vp -m fluid_build.cli providers;'


version: ## Print CLI version
	@bash -lc '\
if [ -x "$(VENV)/bin/python" ]; then vp="$(VENV)/bin/python"; \
elif [ -x "$(VENV)/Scripts/python.exe" ]; then vp="$(VENV)/Scripts/python.exe"; \
else vp=$(PYTHON); fi; \
echo "Using: $$vp"; \
$$vp -m fluid_build.cli version;'

# ============================================================================
# Feature Release System (MVP)
# ============================================================================

.PHONY: show-features build-by-profile build-for-stable build-for-beta build-for-alpha quick-test-profile

show-features: ## 📋 Show feature maturity status and build profiles
	@echo "📋 FLUID Feature Status"
	@echo "======================="
	@bash -lc '\
if [ -x "$(VENV)/bin/python" ]; then vp="$(VENV)/bin/python"; \
elif [ -x "$(VENV)/Scripts/python.exe" ]; then vp="$(VENV)/Scripts/python.exe"; \
else vp=$(PYTHON); fi; \
$$vp scripts/check_features.py;'

build-by-profile: ## 🏗️ Build with specific profile (use PROFILE=stable|beta|alpha)
	@echo "🏗️ Building with profile: $(or $(PROFILE),alpha)"
	@export FLUID_BUILD_PROFILE=$(or $(PROFILE),alpha) && \
		bash -lc '\
if [ -x "$(VENV)/bin/python" ]; then vp="$(VENV)/bin/python"; \
elif [ -x "$(VENV)/Scripts/python.exe" ]; then vp="$(VENV)/Scripts/python.exe"; \
else vp=$(PYTHON); fi; \
$$vp -m build;'
	@echo "✅ Build complete for profile: $(or $(PROFILE),alpha)"
	@ls -lh dist/ 2>/dev/null || echo "No dist/ directory yet"

build-for-stable: ## ✅ Build stable release (production-ready features only)
	@echo "✅ Building STABLE release (production-ready features only)..."
	@$(MAKE) build-by-profile PROFILE=stable

build-for-beta: ## ⚠️ Build beta release (stable + beta features)
	@echo "⚠️ Building BETA release (stable + beta features)..."
	@$(MAKE) build-by-profile PROFILE=beta

build-for-alpha: ## 🔧 Build alpha release (all features for development)
	@echo "🔧 Building ALPHA release (all features)..."
	@$(MAKE) build-by-profile PROFILE=alpha

quick-test-profile: ## 🧪 Quick test of feature detection (use PROFILE=stable|beta|alpha)
	@echo "🧪 Testing profile: $(or $(PROFILE),alpha)"
	@export FLUID_BUILD_PROFILE=$(or $(PROFILE),alpha) && \
		bash -lc '\
if [ -x "$(VENV)/bin/python" ]; then vp="$(VENV)/bin/python"; \
elif [ -x "$(VENV)/Scripts/python.exe" ]; then vp="$(VENV)/Scripts/python.exe"; \
else vp=$(PYTHON); fi; \
$$vp -c "import fluid_build; \
s = fluid_build.get_features_summary(); \
print(f\"Profile: {s[\"profile\"]}\"); \
print(f\"Providers: {s[\"providers\"]}\"); \
print(f\"Commands: {s[\"command_count\"]} enabled\")";'

test-feature-api: ## 🔍 Test feature detection API
	@echo "🔍 Testing feature detection API..."
	@bash -lc '\
if [ -x "$(VENV)/bin/python" ]; then vp="$(VENV)/bin/python"; \
elif [ -x "$(VENV)/Scripts/python.exe" ]; then vp="$(VENV)/Scripts/python.exe"; \
else vp=$(PYTHON); fi; \
$$vp -c "\
import fluid_build;\
print(\"Version:\", fluid_build.__version__);\
print(\"Build Profile:\", fluid_build.get_build_profile());\
print();\
print(\"Enabled Providers:\", sorted(fluid_build.get_enabled_providers()));\
print(\"Enabled Commands:\", len(fluid_build.get_enabled_commands()), \"commands\");\
print();\
print(\"GCP enabled?\", fluid_build.is_provider_enabled(\"gcp\"));\
print(\"AWS enabled?\", fluid_build.is_provider_enabled(\"aws\"));\
print(\"Snowflake status:\", fluid_build.get_feature_status(\"provider\", \"snowflake\"));\
";'

assess-provider: ## 📊 Assess provider maturity (use PROVIDER=gcp LEVEL=stable)
	@echo "📊 Assessing provider: $(or $(PROVIDER),gcp) at level: $(or $(LEVEL),alpha)"
	@bash -lc '\
if [ -x "$(VENV)/bin/python" ]; then vp="$(VENV)/bin/python"; \
elif [ -x "$(VENV)/Scripts/python.exe" ]; then vp="$(VENV)/Scripts/python.exe"; \
else vp=$(PYTHON); fi; \
$$vp scripts/assess_provider.py --provider $(or $(PROVIDER),gcp) --level $(or $(LEVEL),alpha);'

assess-all-providers: ## 📊 Assess all providers at specified level (use LEVEL=alpha)
	@echo "📊 Assessing all providers at level: $(or $(LEVEL),alpha)"
	@bash -lc '\
if [ -x "$(VENV)/bin/python" ]; then vp="$(VENV)/bin/python"; \
elif [ -x "$(VENV)/Scripts/python.exe" ]; then vp="$(VENV)/Scripts/python.exe"; \
else vp=$(PYTHON); fi; \
$$vp scripts/assess_provider.py --provider all --level $(or $(LEVEL),alpha);'

provider-summary: ## 📋 Show summary of all providers
	@echo "📋 Provider maturity summary..."
	@bash -lc '\
if [ -x "$(VENV)/bin/python" ]; then vp="$(VENV)/bin/python"; \
elif [ -x "$(VENV)/Scripts/python.exe" ]; then vp="$(VENV)/Scripts/python.exe"; \
else vp=$(PYTHON); fi; \
$$vp scripts/assess_provider.py --summary;'
# ---- AI/CI Build Logs ----

show-latest-build: ## 📊 Show latest build report
	@echo "📊 Latest Build Report"
	@echo "===================="
	@if [ -d "daily_context_store/build-logs" ]; then \
		LATEST=$$(ls -t daily_context_store/build-logs/ | head -1); \
		if [ -f "daily_context_store/build-logs/$$LATEST/build-report.md" ]; then \
			cat "daily_context_store/build-logs/$$LATEST/build-report.md"; \
		else \
			echo "No report found in $$LATEST"; \
		fi \
	else \
		echo "No build logs directory found. Run a build first."; \
	fi

show-build-failures: ## ❌ Show test failures from latest build
	@echo "❌ Test Failures from Latest Build"
	@echo "=================================="
	@if [ -d "daily_context_store/build-logs" ]; then \
		LATEST=$$(ls -t daily_context_store/build-logs/ | head -1); \
		if [ -f "daily_context_store/build-logs/$$LATEST/test-failures.txt" ]; then \
			cat "daily_context_store/build-logs/$$LATEST/test-failures.txt"; \
		else \
			echo "✅ No test failures in $$LATEST"; \
		fi \
	else \
		echo "No build logs directory found."; \
	fi

show-coverage-trend: ## 📈 Show coverage trend across recent builds
	@echo "📈 Coverage Trend (Last 10 Builds)"
	@echo "=================================="
	@if [ -d "daily_context_store/build-logs" ]; then \
		for dir in $$(ls -t daily_context_store/build-logs/ | head -10); do \
			if [ -f "daily_context_store/build-logs/$$dir/build-report.json" ]; then \
				COV=$$(jq -r '.coverage.overall // "N/A"' "daily_context_store/build-logs/$$dir/build-report.json"); \
				PROFILE=$$(jq -r '.build.profile // "unknown"' "daily_context_store/build-logs/$$dir/build-report.json"); \
				TESTS=$$(jq -r '.test_results.summary.total // "0"' "daily_context_store/build-logs/$$dir/build-report.json"); \
				FAILED=$$(jq -r '.test_results.summary.failed // "0"' "daily_context_store/build-logs/$$dir/build-report.json"); \
				echo "$$dir ($$PROFILE): $$COV% coverage, $$TESTS tests ($$FAILED failed)"; \
			fi \
		done \
	else \
		echo "No build logs found."; \
	fi

compare-builds: ## 🔄 Compare last two builds
	@echo "🔄 Comparing Last Two Builds"
	@echo "==========================="
	@if [ -d "daily_context_store/build-logs" ]; then \
		BUILD1=$$(ls -t daily_context_store/build-logs/ | head -1); \
		BUILD2=$$(ls -t daily_context_store/build-logs/ | head -2 | tail -1); \
		echo "Newer: $$BUILD1"; \
		echo "Older: $$BUILD2"; \
		echo ""; \
		if [ -f "daily_context_store/build-logs/$$BUILD1/build-report.json" ] && \
		   [ -f "daily_context_store/build-logs/$$BUILD2/build-report.json" ]; then \
			echo "Coverage Comparison:"; \
			echo "-------------------"; \
			diff -u \
				<(jq '.coverage.by_provider' "daily_context_store/build-logs/$$BUILD2/build-report.json") \
				<(jq '.coverage.by_provider' "daily_context_store/build-logs/$$BUILD1/build-report.json") \
				|| true; \
			echo ""; \
			echo "Test Results Comparison:"; \
			echo "----------------------"; \
			OLD_TESTS=$$(jq -r '.test_results.summary.total // 0' "daily_context_store/build-logs/$$BUILD2/build-report.json"); \
			NEW_TESTS=$$(jq -r '.test_results.summary.total // 0' "daily_context_store/build-logs/$$BUILD1/build-report.json"); \
			echo "Tests: $$OLD_TESTS → $$NEW_TESTS"; \
		fi \
	else \
		echo "Need at least 2 builds to compare."; \
	fi

list-builds: ## 📋 List all available build logs
	@echo "📋 Available Build Logs"
	@echo "====================="
	@if [ -d "daily_context_store/build-logs" ]; then \
		ls -lt daily_context_store/build-logs/ | grep "^d" | awk '{print $$9}' | while read dir; do \
			if [ -f "daily_context_store/build-logs/$$dir/build-meta.json" ]; then \
				PROFILE=$$(jq -r '.profile // "unknown"' "daily_context_store/build-logs/$$dir/build-meta.json"); \
				TIMESTAMP=$$(jq -r '.timestamp // "unknown"' "daily_context_store/build-logs/$$dir/build-meta.json"); \
				echo "$$dir ($$PROFILE) - $$TIMESTAMP"; \
			else \
				echo "$$dir"; \
			fi \
		done \
	else \
		echo "No build logs found."; \
	fi

ai-analyze-build: ## 🤖 Prepare build report for AI analysis (outputs JSON path)
	@echo "🤖 Latest Build Report for AI Analysis"
	@echo "====================================="
	@if [ -d "daily_context_store/build-logs" ]; then \
		LATEST=$$(ls -t daily_context_store/build-logs/ | head -1); \
		REPORT="daily_context_store/build-logs/$$LATEST/build-report.json"; \
		if [ -f "$$REPORT" ]; then \
			echo "Build: $$LATEST"; \
			echo "Report: $$REPORT"; \
			echo ""; \
			echo "Sample AI Prompts:"; \
			echo "-----------------"; \
			echo "1. Review $$REPORT and suggest fixes for failing tests"; \
			echo "2. Analyze coverage gaps in $$REPORT and recommend tests"; \
			echo "3. Compare this build to previous builds for quality trends"; \
			echo ""; \
			echo "Quick summary:"; \
			jq '{build:.build, summary:.test_results.summary, coverage:.coverage.overall, recommendations:.recommendations}' "$$REPORT"; \
		else \
			echo "No JSON report found"; \
		fi \
	else \
		echo "No build logs found."; \
	fi

cleanup-old-builds: ## 🧹 Clean up build logs older than 30 days
	@echo "🧹 Cleaning up old build logs..."
	@if [ -d "daily_context_store/build-logs" ]; then \
		find daily_context_store/build-logs/ -maxdepth 1 -type d -mtime +30 -exec echo "Removing: {}" \; -exec rm -rf {} \; || true; \
		echo "✅ Cleanup complete"; \
	else \
		echo "No build logs to clean"; \
	fi
