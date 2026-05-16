# Governance

This document describes how the FLUID Forge project is governed.

## Project Roles

### Users

Anyone who uses FLUID Forge. Users are encouraged to participate in the community by:

- Filing bug reports and feature requests via [GitHub Issues](https://github.com/Agenticstiger/forge-cli/issues)
- Joining [GitHub Discussions](https://github.com/Agenticstiger/forge-cli/discussions)
- Helping other users

### Contributors

Anyone who has had a pull request merged into the project. Contributors are listed in the project's git history. All contributions must follow the [Contributing Guide](CONTRIBUTING.md) and [Code of Conduct](CODE_OF_CONDUCT.md).

### Committers

Contributors who have demonstrated sustained, high-quality contributions and a deep understanding of the codebase. Committers have write access to the repository and can:

- Review and merge pull requests
- Triage issues
- Participate in release decisions

**How to become a committer:** Committers are nominated by existing committers and approved by the maintainers. Nominations are based on:

- Quality and consistency of contributions
- Constructive participation in code reviews and discussions
- Understanding of the project's goals and architecture

### Maintainers

Maintainers are responsible for the overall direction of the project. They have admin access to the repository and make final decisions on:

- Project roadmap and priorities
- Releases and versioning
- Governance changes
- New committer appointments

**Current maintainers:**

| Name | GitHub | Role |
|------|--------|------|
| Jeff Watson | [@jeffwatson-ai](https://github.com/jeffwatson-ai) | Project steward, Agentics Transformation Pty Ltd |

### Current Committers

| Name | GitHub |
|------|--------|
| Jeff Watson | [@jeffwatson-ai](https://github.com/jeffwatson-ai) |
| Christopher Ducci | [@doochman](https://github.com/doochman) |
| Marcel Miciak | [@Marcel-Miciak](https://github.com/Marcel-Miciak) |

## Decision-Making

### Day-to-day decisions

Most decisions are made through the normal pull request and code review process. Any committer can merge a PR once it has:

1. At least one approving review from a committer or maintainer
2. All CI checks passing
3. No unresolved review comments

### Significant decisions

For changes that affect the project's direction, public API, or governance, the following process applies:

1. **Proposal** - Open a GitHub Discussion in the "Ideas" category describing the change, its motivation, and any alternatives considered.
2. **Discussion** - Allow at least 7 days for community feedback.
3. **Decision** - Maintainers make the final decision, taking community feedback into account. The decision and rationale are recorded in the discussion thread.

Examples of significant decisions:

- Breaking changes to the CLI or contract specification
- Adding or removing a built-in provider
- Changes to the governance model
- Licensing changes

### Conflict resolution

If consensus cannot be reached through discussion, the maintainers make the final decision. Maintainers commit to:

- Explaining the reasoning behind decisions
- Acting in the best interest of the project and its community
- Revisiting decisions if new information becomes available

## Code Ownership

Code ownership is tracked via the [CODEOWNERS](.github/CODEOWNERS) file. Changes to security-sensitive areas require review from the `@jeffwatson-ai` team.

## Releases

Releases follow [Semantic Versioning](https://semver.org/):

- **Patch** (0.7.x) - Bug fixes, no breaking changes
- **Minor** (0.x.0) - New features, backward-compatible
- **Major** (x.0.0) - Breaking changes (with migration guide)

Releases are published by maintainers via the automated release workflow. See the [CHANGELOG](CHANGELOG.md) for release history.

## Amendments

This governance document may be amended by the maintainers. Significant changes will follow the "Significant decisions" process described above.

---

*This governance model is intentionally lightweight. As the community grows, we will evolve it to ensure fair and transparent decision-making.*
