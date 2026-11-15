# Verification and open work

## Verified in itsflownium/prbot

- GitHub Actions ran the analysis job on ten same-repository test PRs. The original
  template's token received a 403 from the pull-request API on the private repo.
  Adding `pull-requests: read` let the job fetch PR data and upload artifacts.
- PRbot downloaded the real artifacts, set commit statuses and labels, and
  created or updated GitHub comments from a local review runner.
- Hermes Agent served `deepseek/deepseek-v4.1-flash` through Nous Portal.
  PRbot posted review findings and author-facing instructions in comment mode.
- In commit mode a test fix removed one unused import from a test branch. The
  new commit triggered GitHub Actions. The default workflow token resolved to
  `github-actions[bot]` in the analysis job.
- Analysis and CI completions on this repository start the native
  `workflow_run` writer on `main`.

## Remaining integration checks

- Test contributor approval, fork review and permitted fork fixes in a
  repository that accepts forks.
- Verify end-to-end review and fix using a GitHub App installation token. The
  review action accepts an explicit `bot-login` for App installations.
- The live model test used Hermes Agent as the gateway. The vLLM/SGLang example
  defaults and the Claude backend still need authenticated tests against their
  respective services.

## Design limits

- Lint findings are convenience feedback. The analysis workflow runs against
  the PR's linter configuration. Use the repository's authoritative CI checks
  for merge protection.
- Overlap detection searches the configured recent PRs and files per PR. Larger
  limits increase GitHub API calls. Similarity is advisory by default.
- Large repositories should bound scheduled sweeps to fit their Actions and
  GitHub API budgets.
- The repository has no license yet. Choose one before publishing it for reuse.
- Additional linter integrations such as mypy, Clippy, golangci-lint and
  ShellCheck remain future work.
