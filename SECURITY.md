# Security Policy

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| 0.7.x   | :white_check_mark: |
| < 0.7   | :x:                |

## Reporting a Vulnerability

**Please do NOT report security vulnerabilities through public GitHub issues.**

Instead, please report them via email to **change@agenticstransformation.com**.

You should receive a response within **48 hours**. If for some reason you do not, please follow up via email to ensure we received your original message.

Please include the following information (as much as you can provide):

- Type of issue (e.g., credential exposure, injection, privilege escalation)
- Full paths of source file(s) related to the issue
- The location of the affected source code (tag/branch/commit or direct URL)
- Any special configuration required to reproduce the issue
- Step-by-step instructions to reproduce the issue
- Proof-of-concept or exploit code (if possible)
- Impact of the issue, including how an attacker might exploit it

This information will help us triage your report more quickly.

## Disclosure Policy

- We will acknowledge your report within 48 hours.
- We will provide an estimated timeline for a fix within 7 days.
- We will notify you when the vulnerability is fixed.
- We will credit you in the release notes (unless you prefer to remain anonymous).

We follow [coordinated disclosure](https://en.wikipedia.org/wiki/Coordinated_vulnerability_disclosure). We ask that you:

- Give us reasonable time to address the issue before making it public.
- Make a good-faith effort to avoid privacy violations, data destruction, and service disruption.
- Do not access or modify other users' data.

## Security Best Practices for Users

- **Never commit secrets** to your contract files. Use environment variables or a credential resolver.
- **Keep dependencies updated:** `pip install --upgrade data-product-forge`
- **Use the built-in secret scanner:** `detect-secrets scan` (see [CONTRIBUTING.md](CONTRIBUTING.md))
- **Review plans before applying:** Always run `fluid plan` and inspect the output before `fluid apply`.

## Security Features

FLUID Forge includes several built-in security measures:

- **SQL identifier validation** — prevents injection in generated SQL
- **Credential redaction** — secrets are redacted from logs and plan output
- **Provider auth isolation** — each provider manages its own authentication boundary
- **Policy-as-code** — governance rules compile to native cloud IAM before deployment

## Plugin Trust Model

`data-product-forge` discovers external functionality through three Python entry-point groups:

| Entry-point group | Where it hooks | What plugins do |
|---|---|---|
| `fluid_build.commands` | `cli/bootstrap.py` | Register additional `fluid <name>` subcommands. |
| `fluid_build.extension_validators` | `cli/validate.py` | Validate sub-keys of `contract.extensions`. |
| `fluid_build.apply_hooks` | `cli/apply.py` | Apply-time invariant checks (e.g. scaffold bundle digest drift). |

**Plugins are uncontained Python loaded into the same process as the CLI.**
Trust in a plugin equals trust in whatever `pip` resolved when the user
installed it. The CLI does *not* sandbox plugin code, time-limit it, or
restrict what it can import or do with the host filesystem and network.

### What the CLI does defend against

- **Crash containment.** Each plugin's `load()` and invocation is wrapped
  in `try/except`. Plugin exceptions are logged at `WARNING` (bootstrap)
  or folded into the validation/apply error stream — they never crash
  `fluid` itself.
- **Contract mutation.** `_run_apply_hooks` passes each hook a
  `copy.deepcopy()` of the contract, not the live reference, so a buggy
  or malicious hook cannot corrupt the data structure the rest of apply
  consumes. Other plugins in the same run see an untouched contract too.
- **Credential leak in error messages.** Plugin exception text is
  pre-scrubbed with `redact_secret_text` before it reaches log lines or
  the `ValidationResult.errors` list. This covers free-form error
  strings that the placeholder-based `SecretRedactingFilter` cannot
  catch on its own. Test pinning lives in
  `tests/test_cli_plugin_hooks.py::TestPluginErrorRedaction`.
- **Override gate.** `fluid apply --force-pattern-drift` is the only
  flag that downgrades hook errors to warnings; nothing else silently
  ignores an apply hook's verdict.

### What the CLI deliberately does NOT defend against

- **Malicious plugins.** A plugin can read environment variables, write
  arbitrary files, exfiltrate data, or do anything else Python can do.
  Vet packages before installing them.
- **Hung plugins.** A plugin that loops infinitely or blocks on the
  network blocks the CLI. There is no per-hook timeout.
- **Resource exhaustion.** A plugin can allocate unbounded memory or fork
  subprocesses; the CLI has no per-plugin resource caps.

### Reporting a plugin vulnerability

If a vulnerability lives in a *third-party* plugin (not in this CLI),
file the report with that plugin's project. If the issue is in how the
CLI dispatches to plugins — the hook discovery, exception trapping,
contract isolation, or redaction logic — file it here via the channel
above.

## Contact

For security concerns: **change@agenticstransformation.com**

For general questions: [GitHub Discussions](https://github.com/Agenticstiger/forge-cli/discussions)
