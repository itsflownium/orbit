# Working on Orbit

Orbit pre-reviews GitHub pull requests. It runs cheap checks, then either comments the problems so
the author can fix them or fixes them itself, and only marks a PR ready for maintainers once nothing
blocks it. Read [README.md](README.md) for what it does and how it is installed. Open work is listed
in [docs/KNOWN_ISSUES.md](docs/KNOWN_ISSUES.md).

## Setup and checks

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]" ruff
.venv/bin/python -m pytest -q                         # all tests, offline
.venv/bin/ruff check prbot tests --select F,E9,B      # lint
.venv/bin/prbot config examples/hermes-agent.prereview.yml   # validate a config
```

Every change needs passing tests and a clean lint run. Tests that need ruff skip themselves when it is
not on PATH; run with `PATH="$PWD/.venv/bin:$PATH"` to include them. CI (`.github/workflows/test.yml`)
runs the same suite on every push.

The tests never touch the network. `tests/fakes.py` has `FakeGitHub`, an in-memory stand-in for
`prbot.github.GitHub`. When you add a GitHub API call, add it to the fake too, **and** add a test that
drives the real `GitHub` method with a stubbed `request()`. A fake that replaced the real paging
code once hid a bug that broke lint results in production.

Try changes against a real repo read-only with `--dry-run` (it reads from GitHub, writes nothing):

```bash
GITHUB_TOKEN=$(gh auth token) .venv/bin/prbot --dry-run review OWNER/REPO 123
```

## Layout

| Path | What it is |
| --- | --- |
| `prbot/runner.py` | One review: `/bot` commands, checks, fixes, model review, then comment, labels, status. Also the backlog `sweep` and report. |
| `prbot/checks/` | Cheap checks that need only GitHub data (`basic.py`: CI, conflicts, size, tests, description, secrets; `overlap.py`: duplicate PRs). `lint.py` only reads results from the analysis job. |
| `prbot/analyzer.py` | Runs the repo's linters on PR code. Used only in the unprivileged analysis job or locally. Also validates its results (`load_analysis`). |
| `prbot/tools.py` | Linter and formatter table: detection, "is it enforced", version pins, commands. |
| `prbot/patches.py` | Strict parser and renderer for zero-context diffs; keeps only hunks on the PR author's lines. |
| `prbot/fixer.py` | Turns analysis patches into fix commits or GitHub suggestion comments, without running any tools. |
| `prbot/workspace.py` | Hardened git: fetch, checkout, diff, apply, commit, push. |
| `prbot/commenter.py` | The summary comment, its hidden state block, inline comments, labels, text escaping. |
| `prbot/commands.py` | `/bot` comment commands, read from the PR's comments on every run. |
| `prbot/llm.py` | Model providers (Hermes/OpenAI-compatible, Claude) and prompts; validates model output. |
| `prbot/github.py` | Small REST/GraphQL client on the standard library. |
| `prbot/config.py` | Defaults, plus the repo's `.github/prereview.yml` merged in. |
| `prbot/cli.py` | `prbot review`, `sweep`, `event`, `prepare`, `analyze`, `pins`, `config`. |
| `action.yml`, `examples/` | The GitHub Action and the two workflows it needs. |

## Safety rules (do not break these)

The bot runs on public repos that take PRs from strangers. These rules are what keep that safe; each has
tests in `tests/test_review_findings.py` and `tests/test_review_round2.py`.

1. **PR code never runs in the privileged job.** Only `prbot analyze`, in the `pull_request` workflow with a
   read-only token and no secrets, runs linters on PR code. `prbot event` must never call a tool, a build,
   or anything that reads executable config from the PR checkout.
2. **Analysis results are untrusted data.** `load_analysis` must keep checking the repo, PR number and head
   commit. It keeps only `lint-*` findings with plain keys. Patches go through
   `patches.keep_author_hunks`, which rejects anything that isn't strictly shaped and rewrites only lines
   the PR added, with exactly that content. Never pass artifact text straight to `git apply`.
3. **Tools are chosen from the base branch.** Which tools run, whether they're enforced, and which versions
   are pinned are all read before the PR head is checked out.
4. **Only the bot's own summary comment holds state.** `commenter.is_bot_comment` must match the bot's login,
   and the last state block wins. Pass any text that didn't come from the bot itself through
   `commenter.clean()` before posting it: model output, CI logs, titles, dispute reasons, tool output.
5. **Credentials stay out of argv, logs and `.git/config`.** Git gets them through `GIT_CONFIG_*` env vars
   for the single fetch or push that needs them. Redirects to another host drop the `Authorization` header.
6. **Maintainer commands check repo permission through the API** (write, maintain or admin), never
   `author_association`.
7. **Fail closed.** If lint results are missing, the status stays pending. A disputed blocker stays pending
   until a maintainer decides.

## Conventions

- Python 3.10+, standard library first. Runtime dependencies are only `pyyaml` and `certifi`; `anthropic` is
  optional.
- Nothing is hardcoded per repo: new behaviour gets a default in `config.py` and can be overridden in
  `.github/prereview.yml`.
- A finding's `key` must stay the same across runs, because disputes, inline de-duplication and the
  "fixed" checklist all depend on it.
- Model calls are budgeted (`llm.max_reviews_per_run`) and cached per head commit. Keep it that way;
  the point of the project is that it's cheap.
