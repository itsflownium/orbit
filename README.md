# Orbit

<img src="docs/orbit-logo.png" alt="Orbit logo" width="96" height="96">

Orbit pre-reviews GitHub pull requests on any repository. It finds problems, then either
**fixes them** or **comments so the author can fix them**. A PR reaches the maintainer queue
(`bot:ready-for-review`) only once nothing blocks it.

It is built to be cheap. Most checks call no model at all. The model review runs on a self-hosted
**Hermes** model, only on PRs that are already clean, and only up to a budget you set.

## What it does on each PR

1. **Tier 0 checks** (no model): failing CI with the end of the log, merge conflicts, branch behind base,
   PR size, code changes with no tests, empty description or missing linked issue, leaked secrets,
   conflict markers, junk/binary files, and overlap or duplicates with other open PRs.
2. **The repo's own linters and formatters**, auto-detected from the checkout: ruff, ruff-format,
   black, gofmt, rustfmt, prettier, eslint.
   - A tool runs only if the repo's CI, hooks, justfile, Makefile or package scripts actually run it.
     A tool that's configured but never enforced never blocks a PR.
   - It uses the version the repo pins (`pyproject`, requirements files, pre-commit), through `uvx` if needed.
   - It reports only problems on lines the PR adds, never code that was already there.
3. **Fixes** are controlled by the repository's base-branch configuration. Maintainers can choose
   feedback for the author, one-click suggestions, or automatic commits for supported lint fixes.
4. **Hermes review** (budgeted):
   - A small model triages the PR: change type, risk, and whether the description matches the diff.
   - A deeper review checks code for actionable defects. Findings point to added lines and explain
     the condition that causes the problem, the impact, and how to fix and verify it.
   - Findings can link to related code on the reviewed commit. The bot reads nearby source and any
     exact paths listed in `checks.llm_review.context_paths`, then verifies each linked line.
5. **One summary comment, edited in place**, plus inline comments, labels and a `pre-review` commit status.
   It re-checks on every push, and fixed items are ticked off.

For hermes-agent, see [examples/hermes-agent.prereview.yml](examples/hermes-agent.prereview.yml): a config
based on that repo's CI, PR template and lint setup.

Nothing is hardcoded per repository. Defaults live in [prbot/config.py](prbot/config.py). Each repository
can override them in `.github/prereview.yml` (see [examples/prereview.yml](examples/prereview.yml)), which is
read from the **base branch**, so a PR can't change how it gets reviewed.

### Choose how fixes are applied

```yaml
autofix:
  mode: comment
```

- `comment` explains each finding and leaves the author to edit the branch. Any example
  replacement is plain code in the comment. `off` and `enabled: false` also select this behavior.
- `suggest` delivers available patches as GitHub suggestions the author can apply. An authorized
  `/bot fix` can explicitly request a commit.
- `commit` applies supported lint and formatter patches with `PRBOT_PUSH_TOKEN`. It falls back to
  suggestions when the branch cannot be updated. Set `update_branch: false` to keep branch
  synchronization manual. Model findings remain review feedback for the author.

Set `comments.tag_author: true` to mention the PR author in the summary comment.

## Hermes setup

Any OpenAI-compatible server works: vLLM, SGLang, Ollama, llama.cpp or a hosted endpoint.

```bash
vllm serve NousResearch/Hermes-4-70B --port 8000
export PRBOT_LLM_BASE_URL=http://localhost:8000/v1
```

The default model names are the Hugging Face repo IDs:

- triage: `NousResearch/Hermes-4-14B`
- review: `NousResearch/Hermes-4-70B`

If your server exposes different names, set `llm.small_model` and `llm.review_model`. For example,
Ollama's tags look like `hermes3:8b`. One server is enough, because triage falls back to the review model.

How it uses Hermes:

- It never asks for reasoning mode, so responses stay short.
- It removes any `<think>…</think>` block before parsing.
- It puts the JSON schema in the system prompt inside `<schema>` tags.
- If your server rejects `response_format`, it turns JSON mode off and retries.

For Claude, set `llm.provider: anthropic`. Install with `pip install '.[anthropic]'` from the
source checkout and set `ANTHROPIC_API_KEY`. In GitHub Actions, use the action's `anthropic-api-key` input.

## Try it locally (read-only)

```bash
pip install -e .
export GITHUB_TOKEN=...   # any token that can read the repo
```

Print the comment the bot would post on one PR, without writing anything:

```bash
orbit --dry-run review OWNER/REPO 123
```

Scan the backlog and write a report:

```bash
orbit --dry-run sweep OWNER/REPO --limit 50 --report backlog.md
```

To also run linters and fixers, pass a clone of the repo:

```bash
git clone https://github.com/OWNER/REPO /tmp/repo
orbit --dry-run --workspace /tmp/repo review OWNER/REPO 123
```

## Install on a repository (maintainers)

The bot runs as two workflows, so PR code never runs next to a secret:

| Workflow | Trigger | Permissions | What it does |
| --- | --- | --- | --- |
| [prereview-analyze.yml](examples/prereview-analyze.yml) | `pull_request` | read-only token, no secrets | Runs the repo's linters on the PR and uploads the results as an artifact |
| [prereview-workflow.yml](examples/prereview-workflow.yml) | `pull_request_target`, `issue_comment`, `workflow_run`, schedule | write comments, labels, statuses | Reviews, comments, labels and fixes. Never runs PR code; reads the artifact as untrusted data |

1. Copy a trusted Orbit checkout into `.github/actions/prbot/` in the target repository. If you
   host Orbit as a separate action, update the `uses:` lines to a pinned commit the target can access.
2. Copy both example workflows into `.github/workflows/`. Put the review workflow on the default
   branch to receive completion events. List your CI workflow's name under `workflow_run` so the
   bot re-checks when CI finishes. The review workflow queues writes for the repository in one group.
3. Optional secrets (privileged workflow only):
   - `PRBOT_LLM_BASE_URL` and `PRBOT_LLM_API_KEY`: your Hermes endpoint.
   - `PRBOT_PUSH_TOKEN`: a GitHub App or fine-grained token with `contents: write`. It's needed for fix
     commits, because pushes made with the default token don't trigger CI.
   - `PRBOT_ANTHROPIC_API_KEY`: Claude credentials for the optional Claude backend.
4. If the bot posts with a GitHub App token, set the action's `bot-login` input to the app's login
   (e.g. `my-app[bot]`). The bot only trusts summary comments by that login.
5. Start with `dry-run: "true"` in the privileged workflow, read the job logs, then turn it off.

In this repository, `PRBOT_REVIEW_IDENTITY` controls who posts reviews. Both modes use the configured
Hermes endpoint and runner. GitHub keeps a separate summary comment for each identity.

- `bot` is the default. Set the `PRBOT_APP_ID` repository variable and the `PRBOT_APP_PRIVATE_KEY`
  repository secret to post as your GitHub App. With neither configured, GitHub Actions posts the review.
- `user` posts as the GitHub account that owns the token in the `PRBOT_USER_REVIEW_TOKEN` repository
  secret. Use a fine-grained token limited to the repository being reviewed.

Give your App or user token read access to Actions and Contents and write access to Issues,
Pull requests and Commit statuses. Give an App token Checks read access too. GitHub does not offer
Checks access for fine-grained personal tokens, so the installed workflow reads check runs with
GitHub Actions' built-in token in user mode. If you write your own workflow, set the action's
`checks-token` input to a token with Checks read access. The review still posts under the identity
of the token passed as `github-token`.

On a dedicated review runner, the action also reads `PRBOT_LLM_BASE_URL` and `PRBOT_LLM_API_KEY`
from the runner's environment. Explicit action inputs take precedence. With `setup-python: "false"`,
the action builds an isolated environment using the runner's `python3` (3.10 or newer). This
repository's installed workflow uses `PRBOT_REVIEW_RUNNER` as an optional repository variable for a
runner serving Hermes locally when analysis completes.

If you don't run the analysis workflow, set `analysis: {required: false}` in `.github/prereview.yml`.
Otherwise PRs stay "waiting for lint results".

### What the commit status means

| Status | When |
| --- | --- |
| failure | There are blocking findings for the author to fix |
| pending | A required check, lint analysis, or model review needs to finish before the PR is ready. Paused reviews and unresolved disputes over blockers are also pending. |
| success | Nothing blocks (or a maintainer said `/bot ready`), and the PR gets `bot:ready-for-review` |

## Commands (comment on the PR)

Start the comment with a `/bot` command. Maintainers can also recheck a single PR from the
`prereview` workflow's **Run workflow** button on the default branch.

| Command | Who | Effect |
| --- | --- | --- |
| `/bot recheck` | anyone | Run the checks again |
| `/bot fix` | author, maintainer | Commit automatic fixes now |
| `/bot dispute <key> <reason>` | author, maintainer | The finding stops blocking, and the status stays pending until a maintainer decides |
| `/bot ready` / `/bot unready` | maintainer | Override the result |
| `/bot pause` / `/bot resume` | maintainer | Stop or restart the bot on this PR |
| `/bot ignore <check>` | maintainer | Turn a check off for this PR |
| `/bot resolve <key>` | maintainer | Accept a dispute; hide that finding |

"Maintainer" means write, maintain or admin permission on the repo, checked through the API.
Org membership alone is not enough.

## Safety

- The bot can comment, label, set statuses and (with a push token) push fix commits. It never approves,
  merges or closes PRs.
- **Lint results are self-reported by the PR.** The analysis job runs the PR's own workflow file and the
  PR's own tool configs, so a malicious author can hide lint problems from the bot. The repo's real CI
  is still the authority, and the bot's lint check is a convenience for honest contributors. What a
  forged result *can't* do is reach anything else: the bot keeps only lint findings for this PR's
  current commit from the analysis workflow, and only fix hunks that rewrite lines this PR added with
  exactly the content it added.
- **PR code only runs in the analysis job,** which has a read-only token and no secrets. The analysis
  step itself runs with the token removed from its environment. Removing secrets from a process's
  environment is not a security boundary on its own, which is why the jobs are separate.
- **The privileged job never executes PR code.** It applies the analysis patch with `git apply` (hooks and
  fsmonitor off, global and system git config ignored) and keeps only hunks on lines the PR adds,
  recomputed from the GitHub API.
- **Tool choice comes from the base branch:** which linters run, whether they're enforced, and which
  versions are pinned. uv never reads config from the PR checkout (`UV_NO_CONFIG`, `uvx --isolated`).
- `execution` sets which tools run in the analysis job:
  - `never`: none.
  - `same-repo` (default): all tools on branches from the base repo. Fork PRs only get tools that
    don't execute code from the files they check (ruff, black, gofmt). ESLint and Prettier configs can be
    JavaScript, and `cargo fmt` honours a PR's `rust-toolchain.toml`, so those are skipped on forks.
  - `always`: every tool on every PR.
- **The bot only trusts its own summary comment.** Untrusted text (model output, CI logs, dispute reasons,
  titles) is escaped before posting, so it can't fake bot state or @-mention people.
- Credentials reach git through environment variables for the single fetch or push that needs them. They
  never appear in command arguments or logs, and they're dropped when downloads redirect to another host.
- Model output is treated as untrusted. It can only become findings anchored to diff lines, and PR text is
  marked as untrusted in the prompt.
- Deep reviews are capped per run (`llm.max_reviews_per_run`) and cached per commit, so re-runs on the
  same commit cost nothing.
- Running `orbit review --workspace` locally runs the repo's tools on your machine. Use a dedicated clone.
  The bot refuses to run if the clone has uncommitted changes.

## Development

See [AGENTS.md](AGENTS.md) for the layout and the safety rules every change must keep, and
[docs/KNOWN_ISSUES.md](docs/KNOWN_ISSUES.md) for open work.

```bash
pip install -e ".[dev]"
pytest -q
```
