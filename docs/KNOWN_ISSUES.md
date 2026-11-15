# Known issues

All 95 offline tests pass, and CI on `main` is green. These are the open items, highest priority first.
Each says where to look and what "done" means. Read [AGENTS.md](../AGENTS.md) first, especially the
safety rules: every fix must keep them.

## A. Never run for real (verify first)

### A1. The two-workflow setup has only run offline
- **Where:** `action.yml`, `examples/prereview-analyze.yml`, `examples/prereview-workflow.yml`,
  `prbot/github.py` (`analysis_artifacts`, `download_artifact`), `prbot/cli.py` (`cmd_event`, `_event_prs`).
- **Problem:** none of this has run on GitHub Actions: the analysis artifact upload, the privileged job finding
  and downloading it, the job-level concurrency, or the `workflow_run` triggers. The tests use a fake GitHub
  client.
- **Done when:** on a throwaway repo you own, both workflows are installed, and a PR containing an unused
  import gets:
  1. a "waiting for lint results" comment, with status pending
  2. after the analysis job finishes, a ruff finding plus an inline suggestion that deletes the import,
     with status failure
  3. after applying the suggestion, "Pre-review passed" with status success and the `bot:ready-for-review`
     label

  Write down anything that differed from the fakes (response shapes, the `path` field on workflow runs,
  artifact zip layout), and add tests for it.

### A2. Fix commits have never been pushed
- **Where:** `prbot/fixer.py` (commit branch), `prbot/workspace.py` (`checkout_pr`, `apply`, `commit_and_push`).
- **Problem:** commit mode needs `PRBOT_PUSH_TOKEN`, so only suggestion mode has been checked end to end.
- **Done when:** with a fine-grained token (contents: write) set as `PRBOT_PUSH_TOKEN`, the A1 PR gets a fix
  commit pushed by the bot. That push triggers CI and a new review that passes. It works for a same-repo
  branch and for a fork with "Allow edits by maintainers" on.

### A3. The Hermes provider has only run against a fake server
- **Where:** `prbot/llm.py` (`HermesProvider`, `OpenAICompatibleProvider`, `PROVIDER_DEFAULTS`).
- **Problem:** the default model names (`NousResearch/Hermes-4-14B`, `NousResearch/Hermes-4-70B`) are the
  Hugging Face repo IDs, which is what vLLM serves under by default. Nothing has confirmed them against a real
  server or the Nous Portal API, whose base URL and model IDs couldn't be found in its public docs.
- **Done when:** with `PRBOT_LLM_BASE_URL` pointing at a real Hermes server (vLLM, Ollama or Nous Portal), a
  dry-run review of a real PR produces a triage result and a deep review that parse correctly. Then update
  `PROVIDER_DEFAULTS` and the README with model names that are known to work. Also check how the server
  handles `response_format: {"type": "json_object"}` and `<think>` output.

### A4. The Anthropic provider has never run
- **Where:** `prbot/llm.py` (`AnthropicProvider`), `pyproject.toml` (`anthropic>=0.70`).
- **Problem:** the `anthropic` package wasn't installed where this was built, so these calls are unverified:
  `messages.stream(...)`/`beta.messages.stream(...)` with `output_config`, the
  `server-side-fallback-2026-07-01` beta and `fallbacks="default"`. The minimum SDK version is a guess.
- **Done when:** a dry-run review with `ANTHROPIC_API_KEY` set works, and `pyproject.toml` pins the lowest
  SDK version that supports these parameters.

### A5. The bot's login detection is unverified
- **Where:** `prbot/github.py` `login()`.
- **Problem:** with the default `GITHUB_TOKEN`, `/user` fails. The code then tries GraphQL
  `viewer { login }` and falls back to `github-actions[bot]`. Nobody has checked what `viewer` returns for
  `GITHUB_TOKEN` and for GitHub App installation tokens. If it's wrong, the bot can't find its own summary
  comment and posts a new one every run.
- **Done when:** the logins are confirmed in Actions for both token types, and the code needs no
  `bot-login` input in the default-token case.

## B. Design limits (document or improve)

### B1. Lint results are self-reported by the PR
- **Problem:** the analysis job runs the PR's own copy of the workflow file and the PR's own linter config,
  so a malicious author can hide lint problems. This is inherent to running untrusted code without secrets;
  it can't do harm beyond that (see safety rule 2), and the README says so.
- **Possible improvement:** compare against the base branch's linter config. For example, flag a PR that
  changes `ruff.toml`, `pyproject.toml [tool.ruff]`, `.pre-commit-config.yaml` or the analysis workflow, so
  maintainers notice.

### B2. First-time contributors stay pending until a maintainer approves their workflows
- **Problem:** GitHub doesn't run `pull_request` workflows for first-time contributors until a maintainer
  approves them. Until then, the bot keeps the PR "waiting for lint results".
- **Possible improvement:** say so in the comment when the analysis run's status is `action_required`. The
  CI check already sees such runs as `action_required`.

### B3. Overlap detection has a fixed scan size
- **Where:** `prbot/checks/overlap.py` (`max_prs_scanned` 100, GraphQL `files(first: 100)`).
- **Problem:** it only looks at the 100 most recently updated open PRs and the first 100 files of each. Busy
  repos with old duplicates can be missed.
- **Done when:** both limits are configurable and documented, and the GraphQL cost is checked on a repo with
  more than 500 open PRs.

### B4. Fork PRs can be reviewed by two runs at once
- **Where:** `examples/prereview-workflow.yml` (job `concurrency.group`).
- **Problem:** for fork PRs, `workflow_run.pull_requests` is empty, so that trigger's group is the head SHA
  while `pull_request_target` uses the PR number. Two runs can then work on the same PR in parallel. The
  restart-on-change logic in `runner._check_concurrent`/`_publish` keeps the result consistent, but it costs
  an extra review.
- **Possible improvement:** a tiny first job that resolves the PR number (API lookup by head SHA) and hands it
  to the review job's concurrency group.

## C. Smaller items

- **C1. Comment trigger is loose.** The privileged job's `if` uses `contains(comment.body, '/bot')`, so any
  comment that mentions `/bot` starts a run. That's harmless, because commands are parsed strictly, but it
  costs runner minutes. A tighter expression could match `/bot ` at the start of a line.
- **C2. Artifact lookup costs one API call per candidate.** `analysis_artifacts` fetches the workflow run for
  every artifact with a matching head SHA, so heavily re-run PRs cost more calls. Cache runs by id.
- **C3. Same-host https→http redirects keep the token.** `_StripAuthOnRedirect` only compares hosts. GitHub
  never downgrades, but also strip the header when the scheme changes.
- **C4. Only seven tools are auto-detected:** ruff, ruff-format, black, gofmt, rustfmt, prettier and eslint.
  Candidates to add, each with detection, enforcement and pin rules plus tests: mypy, clippy, golangci-lint,
  shellcheck, markdownlint. Keep `safe_untrusted` false for anything whose config can execute code.
- **C5. Local sweeps are slow on big repos.** About 4–5 minutes per 40 PRs, mostly from fetching each PR.
  Fetch in parallel or reuse objects.
- **C6. No license.** Add a `LICENSE` file before making the repo public.
- **C7. Placeholder action references.** `examples/*.yml` use `YOUR-ORG/prbot@main`. Replace them with the
  real location (`itsflownium/prbot@main`), or keep the placeholder and document it.
