# Security Policy

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| 0.7.x   | :white_check_mark: |
| < 0.7   | :x:                |

## Reporting a Vulnerability

**Please do NOT report security vulnerabilities through public GitHub issues.**

Instead, please report them via email to **security@fluidhq.io**.

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

## Contact

For security concerns: **security@fluidhq.io**

For general questions: [GitHub Discussions](https://github.com/Agenticstiger/forge-cli/discussions)
