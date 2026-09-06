"""Product Guardrail profile — forge-cli.

Hand-written and deliberately NOT synced: this is where the repo-specific
facts live so that canon_payload.py and check.py can stay byte-identical
across every repo.

This repo is public, and it publishes twice over — a GitHub Pages site and a
container image. Both carry the company name, and neither is a markdown file.
"""

# The product ships American spelling; the canon is British. Symmetric, so
# "Command Centre" is a finding here too.
LOCALE = "en-US"

SURFACE_TIER = (
    "README.md",
    "docs/",
    "CONTRIBUTING.md",
    "GOVERNANCE.md",
    "SECURITY.md",
    "SUPPORT.md",
    "LICENSE",
    "NOTICE",
    "Dockerfile",
    # CLI help and error text is the copy users read most and no docs scan
    # ever opens. `help=` / `description=` string constants only — never
    # identifiers, never module docstrings.
    "fluid_build/cli/",
)

EXCLUDED = (
    # A changelog records what was said at the time. Rewriting it to a name
    # adopted after that release is falsification, not compliance — and this
    # repo's changelog documents the entity correction itself.
    "CHANGELOG.md",
    # Design spikes and RFCs argue about names on purpose; that is what a
    # proposal is for. They are not what the product says.
    "AUTOGEN_SPIKE.md",
    "RFC-*",
    "docs/archive/",
    ".venv",
    "dist",
    "build",
    "output",
    "*.egg-info",
    "node_modules",
    "site-packages",
    # The guardrail quotes every term it forbids.
    "tools/product_guardrail/",
)

SCAN_ENTITY_FILES = True

# Files that ARE the copy: every string literal is user-facing, not only those
# under a copy-shaped keyword.
PY_ALL_STRINGS = (
    "_error_catalog.py",
    "_agent_voice.py",
)

# Floors: a scan that collapses must fail loudly rather than pass vacuously.
MIN_FILES_SCANNED = 1_240
MIN_BYTES_READ = 16_000_000
MIN_SPANS_EXTRACTED = 53_000
# The floor that matters: characters actually handed to the rules.
# The other three count containers, and all three can be met by a scan
# that extracted nothing.
MIN_TEXT_EXTRACTED = 4_050_000

GRACE = [
    # The site logo reads "FLUID FORGE CLI". The canon is clear that all-caps
    # FORGE is not a product name — but this is a rendered logo on a public
    # homepage, and changing it is a branding decision, not a typo fix. Graced
    # rather than quietly rebranded: the finding prints on every run with a
    # name and a date against it, which is the point of grace.
    {"path": "docs/index.html", "rule": "never-brand",
         "reason": "site logo wordmark; renaming it is a branding call for the owner, "
                "not a lint fix — decide between 'FLUID Forge Engine', plain "
                "'FLUID CLI', or keeping the wordmark as-is",
         "owner": "founder", "decided": "2026-09-06", "expires": "2026-12-31"},
]
