from types import SimpleNamespace

import pytest

from prbot import cli, commenter, llm
from prbot.checks import Context
from prbot.checks.overlap import overlap
from prbot.config import parse_config
from prbot.github import GitHub
from prbot.models import ChangedFile

from .fakes import FakeGitHub, pr_data


REPO = "acme/widget"
FILE = "apps/web/metrics.ts"
COUNTERPART = "services/api/metrics.py"


def test_review_uses_architecture_and_links_the_related_service(monkeypatch):
    gh = FakeGitHub()
    gh.prs[1] = pr_data()
    gh.files[1] = [ChangedFile(FILE, "modified", 1, 1, "@@ -1 +1 @@\n-old\n+new")]
    pr = gh.get_pr(REPO, 1)
    gh.trees[pr.base_sha] = ({path: ("100644", path)
                              for path in (FILE, COUNTERPART, "services/api/main.py", "ARCHITECTURE.md")}, True)
    seen = []

    def read(repo, path, ref):
        seen.append((path, ref))
        return {COUNTERPART: "def metrics():\n    return {'total': 0}",
                "ARCHITECTURE.md": "The web app consumes metrics from the API."}.get(path)

    monkeypatch.setattr(gh, "get_file_text", read)

    class Reviewer:
        def complete_json(self, model, system, user, schema, max_tokens):
            assert "<repository_layout>" in user and "apps/web" in user and "services/api" in user
            assert "The web app consumes metrics from the API." in user
            assert 'File "services/api/metrics.py"\n1 | def metrics():' in user
            return {"findings": [{"path": FILE, "line": 1, "kind": "api-misuse", "confidence": 0.9,
                                  "title": "Metrics contract changed", "references": [
                                      {"path": COUNTERPART, "start_line": 2, "end_line": 2}]}]}

    cfg = parse_config(None)
    findings, reason = llm.deep_review(Reviewer(), cfg, pr, pr.files, None, gh=gh)
    assert reason is None and len(findings) == 1
    assert (COUNTERPART, pr.head_sha) in seen and ("ARCHITECTURE.md", pr.base_sha) in seen
    assert findings[0].references[0].endswith(f"/{COUNTERPART}#L2)")
    body = commenter.render(pr, findings, [], {"posting_method": "GitHub Actions"}, cfg)
    assert "Posted via GitHub Actions" in body and "**Potential blockers**" in body
    assert f"`{FILE}` line 1" in body and "Required checks passed" in body


def test_exact_duplicates_require_the_complete_changes_and_can_match_the_default_branch():
    gh = FakeGitHub()
    gh.prs[1] = pr_data()
    gh.prs[2] = pr_data(number=2, head_sha="c" * 40, title="Independent wording")
    gh.prs[1]["base"]["repo"]["default_branch"] = "main"
    gh.files[1] = gh.files[2] = [ChangedFile(FILE, "modified", 1, 1)]
    pr = gh.get_pr(REPO, 1)
    contents = {FILE: ("100644", "same-object")}
    for sha in (pr.head_sha, pr.base_sha, gh.prs[2]["head"]["sha"]):
        gh.trees[sha] = (contents.copy(), True)
    cfg = parse_config(None)

    def matches():
        return overlap(Context(pr=pr, gh=gh, cfg=cfg))

    findings = matches()
    assert {f.key for f in findings} == {"changes-on-default-branch", "same-changes-open"}
    assert "/pull/2" in next(f.detail for f in findings if f.key == "same-changes-open")
    body = commenter.render(pr, findings, [], {}, cfg)
    assert body.index("These changes are already present on main") < body.index("Pre-review passed")

    gh.trees[pr.base_sha] = (contents, False)
    gh.trees[gh.prs[2]["head"]["sha"]] = ({FILE: ("100755", "same-object")}, True)
    assert {f.key for f in matches()} == {"overlap"}


def test_model_can_see_flag_help_and_the_review_summary_marks_pending():
    gh = FakeGitHub()
    gh.prs[1] = pr_data()
    path = "cli.py"
    gh.files[1] = [ChangedFile(path, "modified", 1, 1, "@@ -1 +1 @@\n-review(args.fresh_model)\n+review(args.force)")]
    gh.repo_files[path] = "review(args.force)\n" + "\n" * 70 + 'parser.add_argument("--force", help="Review drafts")\n'
    pr = gh.get_pr(REPO, 1)
    cfg = parse_config(None)
    context, visible = llm._review_context(gh, cfg, pr, pr.files, [])
    assert '72 | parser.add_argument("--force", help="Review drafts")' in context
    assert 72 in visible[path]
    body = commenter.render(pr, [], [], {"llm_pending": True, "ready": True}, cfg)
    assert "Required checks passed" in body and "Model review is pending" in body


def test_empty_diff_uses_commit_ancestry_for_the_default_branch():
    gh = FakeGitHub()
    gh.prs[1] = pr_data(changed_files=0)
    gh.prs[1]["base"]["repo"]["default_branch"] = "main"
    pr = gh.get_pr(REPO, 1)
    gh.comparisons[(pr.base_sha, pr.head_sha)] = "behind"
    cfg = parse_config(None)
    assert [f.key for f in overlap(Context(pr=pr, gh=gh, cfg=cfg))] == ["changes-on-default-branch"]


def test_tree_and_comparison_use_the_expected_github_api(monkeypatch):
    gh = GitHub(token="fixture")

    def request(method, path, *, params):
        assert method == "GET"
        if path == f"/repos/{REPO}/git/trees/topic%2Fbranch":
            assert params == {"recursive": "1"}
            return {"tree": [{"type": "blob", "path": FILE, "mode": "100644", "sha": "abc"}],
                    "truncated": True}
        assert path == f"/repos/{REPO}/compare/base...head" and params == {"per_page": 1}
        return {"status": "behind"}

    monkeypatch.setattr(gh, "request", request)
    assert gh.get_tree(REPO, "topic/branch") == ({FILE: ("100644", "abc")}, False)
    assert gh.compare_status(REPO, "base", "head") == "behind"


def test_git_credentials_are_scoped_to_the_repository_and_report_the_source(monkeypatch):
    monkeypatch.delenv("GITHUB_SERVER_URL", raising=False)
    monkeypatch.delenv("GITHUB_API_URL", raising=False)
    monkeypatch.delenv("GITHUB_GRAPHQL_URL", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "ignore-environment-token")

    def fill(argv, *, input, env, **kwargs):
        assert argv[-2:] == ["credential", "fill"]
        assert input == f"protocol=https\nhost=github.com\npath={REPO}.git\n\n"
        assert env["GIT_TERMINAL_PROMPT"] == "0" and "GITHUB_TOKEN" not in env
        return SimpleNamespace(stdout="username=person\npassword=stored-token\n")

    monkeypatch.setattr(cli.subprocess, "run", fill)
    assert cli._git_credentials(REPO, None) == ("stored-token", "https://api.github.com")
    assert commenter.posting_method(SimpleNamespace(login=lambda: "person", using_git_credentials=True)) \
        == "Git credentials"
    assert commenter.posting_method(SimpleNamespace(login=lambda: "person")) == "GitHub token"
    monkeypatch.setenv("GITHUB_API_URL", "https://example.com")
    with pytest.raises(ValueError, match="must match"):
        cli._git_credentials(REPO, None)
