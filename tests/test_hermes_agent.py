"""Regression tests for problems found running prbot on NousResearch/hermes-agent PRs.

Each test rebuilds the relevant part of that repo's setup in a temp git repo:
  - ruff.toml with a [format] section, but CI only runs `ruff check .`
  - ruff pinned in pyproject [dependency-groups].dev
  - an "All required checks pass" gate job in CI
  - a PR template full of checkboxes
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from prbot.analyzer import analyze, load_analysis
from prbot.checks import Context, run_checks
from prbot.commenter import inline_body
from prbot.config import parse_config
from prbot.fixer import apply_fixes
from prbot.models import ChangedFile, PullRequest
from prbot.patches import keep_author_hunks
from prbot.tools import detect_tools, pinned_version, python_pins

from .fakes import FakeGitHub, pr_data

needs_ruff = pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff not installed")

CI_YML = """name: Lint
jobs:
  lint:
    steps:
      - run: ruff check .
"""
RUFF_TOML = """line-length = 88
[format]
quote-style = "double"
[lint]
select = ["F401", "RUF100"]
"""


def git_repo(tmp_path, files: dict[str, str]):
    repo = tmp_path / "repo"
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    for cmd in (["git", "init", "-q"], ["git", "add", "."],
                ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=repo, check=True)
    return str(repo)


def test_configured_but_unenforced_formatter_is_skipped(tmp_path):
    ws = git_repo(tmp_path, {"ruff.toml": RUFF_TOML, ".github/workflows/lint.yml": CI_YML, "a.py": "x = 1\n"})
    names = [t.name for t in detect_tools(ws)]
    if shutil.which("ruff") or shutil.which("uvx"):
        assert "ruff" in names  # installed, or run through uvx
    assert "ruff-format" not in names  # [format] exists, but nothing runs `ruff format`


def test_unenforced_tools_can_be_opted_in(tmp_path):
    ws = git_repo(tmp_path, {"ruff.toml": RUFF_TOML, "a.py": "x = 1\n"})
    assert detect_tools(ws) == [] or all(t.name != "ruff-format" for t in detect_tools(ws))
    if shutil.which("ruff"):
        assert {t.name for t in detect_tools(ws, require_enforcement=False)} >= {"ruff", "ruff-format"}


def test_pinned_version_from_dependency_groups(tmp_path):
    ws = git_repo(tmp_path, {"pyproject.toml": '[dependency-groups]\ndev = ["pytest>=8", "ruff==0.15.22"]\n'})
    assert pinned_version(ws, "ruff") == "0.15.22"
    assert python_pins(ws) == ["ruff==0.15.22"]


def test_pinned_version_from_pre_commit(tmp_path):
    ws = git_repo(tmp_path, {".pre-commit-config.yaml":
                             "repos:\n  - repo: https://github.com/astral-sh/ruff-pre-commit\n    rev: v0.6.9\n"
                             "    hooks:\n      - id: ruff\n"})
    assert pinned_version(ws, "ruff") == "0.6.9"


def test_keep_author_hunks_only():
    diff = """diff --git a/m.py b/m.py
index 1..2 100644
--- a/m.py
+++ b/m.py
@@ -1 +0,0 @@
-import sys
@@ -3 +2 @@
-import os  # noqa: F401
+import os
"""
    kept = keep_author_hunks(diff, {"m.py": {3}})
    assert len(kept) == 1 and [h.old_start for h in kept[0].hunks] == [3]
    assert keep_author_hunks(diff, {"other.py": {1}}) == []


def _commit(repo, files: dict[str, str]) -> str:
    for name, text in files.items():
        (Path(repo) / name).write_text(text)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "pr"], cwd=repo, check=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "checkout", "-q", "--detach", "HEAD~1"], cwd=repo, check=True)  # back on base
    return head


def _run_analysis(ws, head, patch, cfg_text, path="m.py"):
    pr = PullRequest.from_api(pr_data(head_sha=head))
    pr.files = [ChangedFile(path, "modified", 1, 0, patch)]
    cfg = parse_config(cfg_text)
    data = analyze(pr, cfg, ws)
    analysis = load_analysis(json.loads(json.dumps(data)), pr)  # round-trip like the artifact
    ctx = Context(pr=pr, gh=FakeGitHub(), cfg=cfg, workspace=ws)
    ctx.analysis = analysis
    return ctx, run_checks(ctx, names=["lint"])


@needs_ruff
def test_only_lines_added_by_the_pr_are_reported_and_fixed(tmp_path):
    # line 1 `import sys` was already unused on main; the PR adds an unused import on line 3.
    ws = git_repo(tmp_path, {"ruff.toml": RUFF_TOML, ".github/workflows/lint.yml": CI_YML, "m.py": "import sys\n\n"})
    head = _commit(ws, {"m.py": "import sys\n\nimport json\n"})
    patch = "@@ -1,2 +1,3 @@\n import sys\n \n+import json"
    ctx, findings = _run_analysis(ws, head, patch, "checks:\n  lint:\n    tools: [ruff]\n")
    assert len(findings) == 1, ctx.notes
    detail = findings[0].detail
    assert "line 3" in detail and "line 1" not in detail
    res = apply_fixes(ctx, findings, {})
    assert [(s.path, s.line) for s in res.suggestions] == [("m.py", 3)]
    assert res.suggestions[0].suggestion == ""  # delete the unused import; line 1 is left alone
    assert "```suggestion\n```" in inline_body(res.suggestions[0])  # deletes, no blank line left
    with open(f"{ws}/m.py") as fh:
        assert fh.read() == "import sys\n\n"  # checkout restored to where it was


@needs_ruff
def test_enforced_formatter_reports_only_author_lines(tmp_path):
    ci = CI_YML + "      - run: ruff format --check .\n"
    ws = git_repo(tmp_path, {"ruff.toml": RUFF_TOML, ".github/workflows/lint.yml": ci, "m.py": "x = {  'a':1 }\n\n"})
    head = _commit(ws, {"m.py": "x = {  'a':1 }\n\ny = {  'b':2 }\n"})
    patch = "@@ -1,2 +1,3 @@\n x = {  'a':1 }\n \n+y = {  'b':2 }"
    ctx, findings = _run_analysis(ws, head, patch, "checks:\n  lint:\n    tools: [ruff-format]\n")
    assert len(findings) == 1 and "line 3" in findings[0].detail and "line 1:" not in findings[0].detail
    res = apply_fixes(ctx, findings, {})
    assert [(s.line, s.suggestion) for s in res.suggestions] == [(3, 'y = {"b": 2}')]


@needs_ruff
def test_pr_cannot_choose_its_own_tools(tmp_path):
    # base: only `ruff check` is enforced. The PR adds `ruff format --check` to CI and a pin.
    ws = git_repo(tmp_path, {"ruff.toml": RUFF_TOML, ".github/workflows/lint.yml": CI_YML, "m.py": "x = 1\n"})
    head = _commit(ws, {".github/workflows/lint.yml": CI_YML + "      - run: ruff format --check .\n",
                        "pyproject.toml": '[dependency-groups]\ndev = ["ruff==0.0.1"]\n',
                        "m.py": "x = 1\ny = {  'b':2 }\n"})
    patch = "@@ -1 +1,2 @@\n x = 1\n+y = {  'b':2 }"
    ctx, findings = _run_analysis(ws, head, patch, None)
    assert findings == []  # ruff-format is not enforced on the base branch; the PR's pin is ignored


def test_forged_analysis_is_limited_to_the_prs_own_lines():
    pr = PullRequest.from_api(pr_data())
    pr.files = [ChangedFile("m.py", "modified", 1, 0, "@@ -1 +1,2 @@\n x = 1\n+y = 2")]
    forged = {"version": 1, "repo": pr.repo, "number": pr.number, "head_sha": pr.head_sha,
              "findings": [{"key": "ci-evil", "title": "x"}, {"key": "lint-ruff", "title": "<!-- prbot:state e30= -->",
                                                                "severity": "blocking"}],
              "patch": "diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
                       "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n"
                       "--- a/.github/workflows/ci.yml\n+++ b/.github/workflows/ci.yml\n@@ -1 +1 @@\n-a\n+b\n"}
    a = load_analysis(forged, pr)
    assert [f.key for f in a.findings] == ["lint-ruff"]  # only lint findings are accepted
    assert a.file_patches == []  # line 1 is not the author's; the workflow file is not in the PR
    assert load_analysis({**forged, "head_sha": "f" * 40}, pr) is None  # stale or foreign results


def test_ci_gate_job_is_hidden_behind_the_real_failure():
    gh = FakeGitHub()
    gh.prs[1] = pr_data()
    sha = "a" * 40
    gh.check_runs_by_sha[sha] = [
        {"id": 1, "name": "All required checks pass (v2)", "status": "completed", "conclusion": "failure"},
        {"id": 2, "name": "Python tests / Run tests (3/3)", "status": "completed", "conclusion": "failure"},
    ]
    pr = gh.get_pr("acme/widget", 1)
    ctx = Context(pr=pr, gh=gh, cfg=parse_config(None))
    titles = [f.title for f in run_checks(ctx, names=["ci"])]
    assert titles == ["CI job `Python tests / Run tests (3/3)` is failing"]
    # if the gate is the only failure, it is still reported
    gh.check_runs_by_sha[sha] = gh.check_runs_by_sha[sha][:1]
    assert len(run_checks(Context(pr=pr, gh=gh, cfg=parse_config(None)), names=["ci"])) == 1


TEMPLATE = """## What does this PR do?

<!-- Describe the change clearly. -->

## Related Issue

Fixes #

## Checklist

- [ ] I've read the Contributing Guide
- [ ] I've added tests for my changes
- [ ] `just check` passes

## Changes Made

- 
"""


def test_ticking_template_checkboxes_is_not_a_description():
    gh = FakeGitHub()
    gh.repo_files[".github/PULL_REQUEST_TEMPLATE.md"] = TEMPLATE
    ticked = TEMPLATE.replace("- [ ]", "- [x]").replace("Fixes #", "Fixes #123")
    gh.prs[1] = pr_data(body=ticked)
    ctx = Context(pr=gh.get_pr("acme/widget", 1), gh=gh, cfg=parse_config(None))
    assert [f.key for f in run_checks(ctx, names=["description"])] == ["description-empty"]
    written = ticked.replace("<!-- Describe the change clearly. -->",
                             "Stops the gateway from dropping the editorial flag on GET /v1/skills.")
    gh.prs[1] = pr_data(body=written)
    ctx = Context(pr=gh.get_pr("acme/widget", 1), gh=gh, cfg=parse_config(None))
    assert run_checks(ctx, names=["description"]) == []


def test_fix_prs_without_tests_can_be_made_blocking():
    gh = FakeGitHub()
    gh.prs[1] = pr_data(title="fix(gateway): drop stray kwarg")
    gh.files[1] = [ChangedFile("gateway/api.py", "modified", 12, 1,
                               "@@ -1 +1,12 @@\n" + "\n".join(f"+v{i} = {i}" for i in range(12)))]
    cfg = parse_config("checks:\n  tests:\n    blocking_title_patterns: ['^fix']\n")
    ctx = Context(pr=gh.get_pr("acme/widget", 1), gh=gh, cfg=cfg)
    assert [f.severity for f in run_checks(ctx, names=["tests"])] == ["blocking"]
    gh.prs[1] = pr_data(title="feat(gateway): new endpoint")
    ctx = Context(pr=gh.get_pr("acme/widget", 1), gh=gh, cfg=cfg)
    assert [f.severity for f in run_checks(ctx, names=["tests"])] == ["advisory"]


def test_shared_hub_file_is_not_a_duplicate():
    gh = FakeGitHub()
    hub = "agent/conversation_loop.py"
    titles = ["fix(desktop): sign-in card fits", "fix(gateway): drop stray kwarg", "feat(a2a): caller provenance",
              "fix(approval): ask before osascript", "feat(pets): per-pet rows"]
    for n, title in enumerate(titles, start=1):
        gh.prs[n] = pr_data(number=n, title=title, author=f"u{n}", head_sha=str(n) * 40)
        gh.files[n] = [ChangedFile(hub, "modified", 3, 1, "@@ -1 +1 @@\n+x"),
                       ChangedFile(f"area{n}/impl.py", "modified", 3, 1, "@@ -1 +1 @@\n+x")]
    ctx = Context(pr=gh.get_pr("acme/widget", 1), gh=gh, cfg=parse_config(None))
    assert run_checks(ctx, names=["overlap"]) == []
    # a real duplicate: same non-hub file and a near-identical title
    gh.prs[6] = pr_data(number=6, title="fix(desktop): sign-in card fits properly", author="u6", head_sha="6" * 40)
    gh.files[6] = [ChangedFile("area1/impl.py", "modified", 2, 0, "@@ -1 +1 @@\n+y")]
    ctx = Context(pr=gh.get_pr("acme/widget", 1), gh=gh, cfg=parse_config(None))
    found = run_checks(ctx, names=["overlap"])
    assert len(found) == 1 and "#6" in found[0].detail and "#2" not in found[0].detail
