const DOCS_LINK_PATTERN = /github\.com\/Agenticstiger\/forge_docs\/pull\/\d+/i;
const NO_DOCS_NEEDED_PATTERN = /- \[x\] \*\*No docs needed\*\*/i;
const DOCS_TODO_PATTERN = /- \[x\] \*\*Docs TODO\*\*/i;
const REMINDER_MARKER = "Documentation Reminder";

const DOCS_ONLY_EXACT_FILES = new Set([
  "README.md",
  "CONTRIBUTING.md",
  "CODE_OF_CONDUCT.md",
  "SECURITY.md",
  "GOVERNANCE.md",
  "SUPPORT.md",
  ".github/pull_request_template.md",
]);

const DOCS_ONLY_PREFIXES = ["docs/", ".github/PULL_REQUEST_TEMPLATE/"];

function extractDocumentationSignals(body = "") {
  return {
    hasDocsLink: DOCS_LINK_PATTERN.test(body),
    noDocsNeeded: NO_DOCS_NEEDED_PATTERN.test(body),
    docsTodo: DOCS_TODO_PATTERN.test(body),
  };
}

function isDocsOnlyChange(files) {
  if (!files.length) {
    return false;
  }

  return files.every((file) => {
    const filename = typeof file === "string" ? file : file.filename;
    return (
      DOCS_ONLY_EXACT_FILES.has(filename) ||
      DOCS_ONLY_PREFIXES.some((prefix) => filename.startsWith(prefix))
    );
  });
}

async function listAllPrFiles({ github, owner, repo, pullNumber }) {
  return github.paginate(github.rest.pulls.listFiles, {
    owner,
    repo,
    pull_number: pullNumber,
    per_page: 100,
  });
}

async function listAllIssueLabels({ github, owner, repo, issueNumber }) {
  return github.paginate(github.rest.issues.listLabelsOnIssue, {
    owner,
    repo,
    issue_number: issueNumber,
    per_page: 100,
  });
}

async function listAllIssueComments({ github, owner, repo, issueNumber }) {
  return github.paginate(github.rest.issues.listComments, {
    owner,
    repo,
    issue_number: issueNumber,
    per_page: 100,
  });
}

function isDocumented({ body, files }) {
  const signals = extractDocumentationSignals(body);
  return signals.hasDocsLink || signals.noDocsNeeded || signals.docsTodo || isDocsOnlyChange(files);
}

async function runDocsReminder({ github, context, core = console }) {
  const pr = context.payload.pull_request;
  if (!pr) {
    core.info?.("No pull request payload found; skipping docs reminder.");
    return { documented: true, skipped: true };
  }

  const body = pr.body || "";
  const { owner, repo } = context.repo;
  const pullNumber = pr.number;

  const files = await listAllPrFiles({ github, owner, repo, pullNumber });
  const documented = isDocumented({ body, files });

  const labels = await listAllIssueLabels({
    github,
    owner,
    repo,
    issueNumber: pullNumber,
  });
  const hasNeedsDocsLabel = labels.some((label) => label.name === "needs-docs");

  if (!documented) {
    if (!hasNeedsDocsLabel) {
      await github.rest.issues.addLabels({
        owner,
        repo,
        issue_number: pullNumber,
        labels: ["needs-docs"],
      });
    }

    const comments = await listAllIssueComments({
      github,
      owner,
      repo,
      issueNumber: pullNumber,
    });
    const alreadyCommented = comments.some(
      (comment) => comment.user?.type === "Bot" && comment.body?.includes(REMINDER_MARKER)
    );

    if (!alreadyCommented) {
      await github.rest.issues.createComment({
        owner,
        repo,
        issue_number: pullNumber,
        body: [
          "### 📄 Documentation Reminder",
          "",
          "This PR appears to be missing a documentation reference. Our docs live in a [separate repo](https://github.com/Agenticstiger/forge_docs).",
          "",
          "Please update the PR description with one of:",
          '- **Link a docs PR** — check the "Docs PR linked" box and paste the URL',
          '- **Mark as no docs needed** — check "No docs needed" with a justification',
          '- **Acknowledge docs TODO** — check "Docs TODO" and create the docs PR before merge',
          "",
          "See the [Contributing Guide](https://github.com/Agenticstiger/forge-cli/blob/main/CONTRIBUTING.md#documentation-requirements) for details.",
        ].join("\n"),
      });
    }
  } else if (hasNeedsDocsLabel) {
    await github.rest.issues.removeLabel({
      owner,
      repo,
      issue_number: pullNumber,
      name: "needs-docs",
    });
  }

  return {
    documented,
    fileCount: files.length,
  };
}

module.exports = {
  DOCS_ONLY_EXACT_FILES,
  DOCS_ONLY_PREFIXES,
  extractDocumentationSignals,
  isDocsOnlyChange,
  isDocumented,
  listAllPrFiles,
  runDocsReminder,
};
