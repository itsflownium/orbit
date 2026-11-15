# Verification and open work

## Verified in itsflownium/orbit

- GitHub Actions ran the analysis job on ten same-repository test PRs. The original
  template's token received a 403 from the pull-request API on the private repo.
  Adding `pull-requests: read` let the job fetch PR data and upload artifacts.
- Orbit downloaded the real artifacts, set commit statuses and labels, and
  created or updated GitHub comments from a local review runner.
- Hermes Agent served `deepseek/deepseek-v4.1-flash` through Nous Portal.
  Orbit posted review findings and author-facing instructions in comment mode.
- In commit mode a test fix removed one unused import from a test branch. The
  new commit triggered GitHub Actions. The default workflow token resolved to
  `github-actions[bot]` in the analysis job.
- Analysis and CI completions on this repository start the native
  `workflow_run` writer on `main`.
- A VM timer triggered a catch-up review of PR #43 on its original commit.
  Orbit posted as the GitHub Actions bot using DeepSeek through Nous Portal.

## Verified locally

- A fresh Python environment installed Orbit both from a wheel and as an
  editable checkout. The `orbit` command loaded both sample configurations.
  Both example workflows resolved the vendored composite action, and its
  install step ran in separate job-local environments.
- A local Git fixture verified that Orbit posts a suggestion when a fork
  disallows edits and commits a fix to the feature branch when it allows them.
  REST fixtures verified separate summary comments for distinct GitHub App identities.

## Remaining integration checks

- Test contributor approval, fork review and permitted fork fixes in a
  repository that accepts forks.
- Verify end-to-end review and fix using a GitHub App installation token. The
  review action accepts an explicit `bot-login` for App installations.
- The live model test used Hermes Agent as the gateway. The vLLM/SGLang example
  defaults and the Anthropic provider still need authenticated tests against
  their respective services.
- The native GitHub cron has yet to record a run.

## Design limits

- Lint findings are convenience feedback. The analysis workflow runs against
  the PR's linter configuration. Use the repository's authoritative CI checks
  for merge protection.
- Overlap detection searches the configured recent PRs and files per PR. Larger
  limits increase GitHub API calls. Similarity is advisory by default.
- Large repositories should bound scheduled sweeps to fit their Actions and
  GitHub API budgets.
- Additional linter integrations such as mypy, Clippy, golangci-lint and
  ShellCheck remain future work.
