"""Readiness checks and regressions for interrupted GitHub writes."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from prbot import analyzer, cli, commenter, tools
from prbot.github import GitHub, GitHubError
from prbot.models import ChangedFile
from prbot.runner import review_pr, sweep
from prbot.workspace import Workspace

from .fakes import FakeGitHub, pr_data
from .test_hermes_agent import CI_YML, RUFF_TOML, _commit, git_repo, needs_ruff


REPO = "acme/widget"


def _pr(*, required=True):
    gh = FakeGitHub()
    gh.prs[1] = pr_data()
    gh.files[1] = [ChangedFile("m.py", "added", 1, 0, "@@ -0,0 +1 @@\n+x = 1")]
    gh.repo_files[".github/prereview.yml"] = f"analysis:\n  required: {str(required).lower()}\n"
    return gh


def _upload(gh, findings=(), patch=""):
    pr = gh.get_pr(REPO, 1)
    data = {
        "version": analyzer.VERSION,
        "repo": pr.repo,
        "number": pr.number,
        "head_sha": pr.head_sha,
        "base_sha": pr.base_sha,
        "complete": True,
        "findings": list(findings),
        "patch": patch,
    }
    gh.artifacts[pr.head_sha] = [{analyzer.ARTIFACT_FILE: json.dumps(data).encode()}]


def test_review_picks_up_late_required_artifact():
    gh = _pr()
    assert review_pr(gh, REPO, 1)["status"] == "pending"
    _upload(gh)
    assert review_pr(gh, REPO, 1)["status"] == "success"


def test_forced_same_head_review_prefers_new_artifact():
    gh = _pr()
    _upload(gh)
    assert review_pr(gh, REPO, 1)["status"] == "success"
    _upload(gh, [{"key": "lint-ruff", "title": "Unused import", "severity": "blocking"}])
    assert review_pr(gh, REPO, 1, force=True)["status"] == "failure"


def test_old_base_artifact_does_not_pass_lint_gate():
    gh = _pr()
    _upload(gh)
    assert review_pr(gh, REPO, 1)["status"] == "success"
    gh.prs[1]["base"]["sha"] = "c" * 40
    assert review_pr(gh, REPO, 1)["status"] == "pending"


def test_overlap_is_refreshed_after_another_pr_is_closed(monkeypatch):
    gh = _pr(required=False)
    gh.repo_files[".github/prereview.yml"] += "checks:\n  overlap:\n    severity: blocking\n"
    peers = [{"number": 2, "title": "Similar change", "files": {"nodes": [{"path": "m.py"}]}}]
    monkeypatch.setattr(gh, "graphql", lambda *args: {"data": {"repository": {"pullRequests": {
        "nodes": peers,
        "pageInfo": {"hasNextPage": False},
    }}}})
    assert review_pr(gh, REPO, 1)["status"] == "failure"
    peers.clear()
    assert review_pr(gh, REPO, 1)["status"] == "success"


def test_eslint_builtin_json_parser_handles_absolute_paths_and_warnings(tmp_path):
    tool = next(t for t in tools.TOOLS if t.name == "eslint")
    output = json.dumps([{"filePath": str(tmp_path / "src.js"), "messages": [
        {"severity": 2, "line": 3, "ruleId": "no-undef", "message": "example is not defined"},
        {"severity": 1, "line": 4, "ruleId": "max-lines", "message": "Long file"},
    ]}])
    assert tool.check_args(["src.js"]) == ["--format", "json", "src.js"]
    assert tool.parse(output, ["src.js"], str(tmp_path)) == [("src.js", 3, "no-undef example is not defined")]


@pytest.mark.parametrize(("status", "conclusion"), [("in_progress", None), ("completed", "action_required")])
def test_blocking_ci_awaits_a_final_result(status, conclusion):
    gh = _pr(required=False)
    sha = gh.prs[1]["head"]["sha"]
    gh.check_runs_by_sha[sha] = [{"id": 1, "name": "test", "status": status, "conclusion": conclusion}]
    assert review_pr(gh, REPO, 1)["status"] == "pending"
    assert "bot:ready-for-review" not in gh.labels.get(1, set())
    assert commenter.decode_state(gh.summary(1))["ci_pending"]

    gh.check_runs_by_sha[sha] = [{"id": 1, "name": "test", "status": "completed", "conclusion": "success"}]
    assert review_pr(gh, REPO, 1)["status"] == "success"
    assert "bot:ready-for-review" in gh.labels[1]


def _run_analyze_cli(monkeypatch, tmp_path, gh, workspace, *, fetched):
    for name in ("GITHUB_TOKEN", "GH_TOKEN", "PRBOT_PUSH_TOKEN", "ACTIONS_RUNTIME_TOKEN",
                 "ACTIONS_ID_TOKEN_REQUEST_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    pr = gh.get_pr(REPO, 1)
    prepared = tmp_path / "pr.json"
    output = tmp_path / analyzer.ARTIFACT_FILE
    prepared.write_text(json.dumps({
        "pr": pr.raw,
        "files": [vars(f) for f in pr.files],
        "fetched": fetched,
        "config": {"checks": {"lint": {"tools": ["ruff"]}}},
    }))
    result = cli.cmd_analyze(SimpleNamespace(pr_json=prepared, workspace=workspace, out=output), None)
    if output.exists():
        gh.artifacts[pr.head_sha] = [{analyzer.ARTIFACT_FILE: output.read_bytes()}]
    return result, output


def test_missing_head_cannot_publish_empty_success(monkeypatch, tmp_path):
    ws = git_repo(tmp_path, {"m.py": "x = 1\n"})
    gh = _pr()
    gh.prs[1]["base"]["sha"] = Workspace(ws).head()
    code, output = _run_analyze_cli(monkeypatch, tmp_path, gh, ws, fetched=False)
    assert code == 2 and not output.exists()
    assert review_pr(gh, REPO, 1)["status"] == "pending"


@needs_ruff
@pytest.mark.parametrize("bad_config", [False, True])
def test_ruff_gate_distinguishes_old_diagnostics_from_incomplete_lint(monkeypatch, tmp_path, bad_config):
    ws = git_repo(tmp_path, {
        "ruff.toml": RUFF_TOML,
        ".github/workflows/lint.yml": CI_YML,
        "m.py": "import json\n",
    })
    edits = {"m.py": "import json\n\nx = 1\n"}
    if bad_config:
        edits["ruff.toml"] = "invalid configuration"
    head = _commit(ws, edits)
    gh = _pr()
    gh.prs[1]["head"]["sha"] = head
    gh.prs[1]["base"]["sha"] = Workspace(ws).head()
    gh.files[1] = [ChangedFile("m.py", "modified", 2, 0, "@@ -1 +1,3 @@\n import json\n+\n+x = 1")]

    code, output = _run_analyze_cli(monkeypatch, tmp_path, gh, ws, fetched=True)
    assert code == (2 if bad_config else 0)
    assert output.exists() == (not bad_config)
    assert review_pr(gh, REPO, 1)["status"] == ("pending" if bad_config else "success")


def test_transient_status_write_is_retried_by_next_sweep(monkeypatch):
    gh = _pr(required=False)
    assert review_pr(gh, REPO, 1)["status"] == "success"
    sha = gh.prs[1]["head"]["sha"]
    gh.check_runs_by_sha[sha] = [{"id": 1, "name": "test", "status": "completed", "conclusion": "failure"}]

    def fail_status(*args, **kwargs):
        raise GitHubError(503, "Test write failure", "/statuses")

    with monkeypatch.context() as patch:
        patch.setattr(gh, "set_status", fail_status)
        assert sweep(gh, REPO)[0]["error"]
    assert sweep(gh, REPO)[0]["status"] == "failure"
    assert gh.status_writes[-1]["state"] == "failure"


@pytest.mark.parametrize("ignored", [False, True])
def test_checkout_preserves_local_untracked_file_on_collision(tmp_path, ignored):
    files = {"tracked.py": "x = 1\n"}
    if ignored:
        files[".gitignore"] = "personal.py\n"
    ws = git_repo(tmp_path, files)
    personal = Path(ws, "personal.py")
    personal.write_text("from_pr = True\n")
    subprocess.run(["git", "add", "-f", "personal.py"], cwd=ws, check=True)
    head = _commit(ws, {})
    original = Workspace(ws).head()
    personal.write_text("local_only = True\n")

    assert not Workspace(ws).checkout(head)
    assert personal.read_text() == "local_only = True\n"
    assert Workspace(ws).head() == original


def test_combined_status_collects_every_page(monkeypatch):
    gh = GitHub(token="offline-fixture")
    next_url = "https://api.github.example/repos/acme/widget/commits/sha/status?page=2"

    def request(method, path, **kwargs):
        assert method == "GET"
        if path == next_url:
            gh._last_link = ""
            return {"statuses": [{"context": "integration", "state": "failure"}]}
        assert path == "/repos/acme/widget/commits/sha/status"
        assert kwargs["params"] == {"per_page": 100}
        gh._last_link = f'<{next_url}>; rel="next"'
        return {"state": "failure", "statuses": [{"context": "lint", "state": "success"}]}

    monkeypatch.setattr(gh, "request", request)
    combined = gh.combined_status(REPO, "sha")
    assert combined == {
        "state": "failure",
        "statuses": [{"context": "lint", "state": "success"}, {"context": "integration", "state": "failure"}],
    }


def test_fork_completion_filters_server_side_and_matches_the_exact_head(monkeypatch):
    gh = GitHub(token="offline-fixture")
    sha = "a" * 40
    run = {
        "head_sha": sha,
        "head_branch": "updates/fix",
        "head_repository": {"full_name": "contributor/widget"},
    }

    def request(method, path, **kwargs):
        assert method == "GET" and path == "/repos/acme/widget/pulls"
        assert kwargs["params"]["head"] == "contributor:updates/fix"
        assert kwargs["params"]["state"] == "open"
        return [
            {"number": i, "head": {"ref": ref, "sha": head_sha, "repo": {"full_name": repo}}}
            for i, ref, head_sha, repo in (
                (1, "updates/fix", sha, "contributor/widget"),
                (2, "updates/fix", "b" * 40, "contributor/widget"),
                (3, "different", sha, "contributor/widget"),
                (4, "updates/fix", sha, "different/widget"),
            )
        ]

    monkeypatch.setattr(gh, "request", request)
    assert cli._event_prs(gh, REPO, run) == [1]


def test_workflow_run_reports_any_review_error_and_runs_other_matches(monkeypatch, tmp_path, capsys):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"workflow_run": {"id": 17}}))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_run")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setattr(cli, "_event_prs", lambda gh, repo, run: [1, 2])
    monkeypatch.setattr(cli, "review_pr", lambda gh, repo, number, **kwargs: {
        "number": number, **({"error": "write failed"} if number == 1 else {"ready": True}),
    })

    args = SimpleNamespace(workspace=None, verbose=False, dry_run=False)
    assert cli.cmd_event(args, _pr()) == 1
    output = capsys.readouterr().out
    assert "ERROR write failed" in output and "#2 : READY" in output


def test_fix_mode_leaves_a_dirty_workspace_intact(monkeypatch, tmp_path):
    ws = git_repo(tmp_path, {"m.py": "x = 1\n"})
    local = Path(ws, "m.py")
    local.write_text("x = 2\n")
    base = Workspace(ws).current_ref()
    gh = _pr()
    gh.repo_files[".github/prereview.yml"] = "autofix:\n  mode: commit\nllm:\n  provider: none\n"
    patch = "diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n@@ -1 +0,0 @@\n-x = 1\n"
    _upload(gh, [{"key": "lint-ruff", "title": "Delete the added line", "severity": "blocking"}], patch)
    monkeypatch.setenv("PRBOT_PUSH_TOKEN", "offline-fixture")

    def unexpected_fetch(*args, **kwargs):
        pytest.fail("Fix mode attempted a fetch from a dirty workspace")

    monkeypatch.setattr(Workspace, "fetch_pr", unexpected_fetch)
    result = review_pr(gh, REPO, 1, workspace=ws)

    assert result["status"] == "failure"
    assert not result.get("pushed")
    assert "Posting available fixes as suggestions." in gh.summary(1)
    assert local.read_text() == "x = 2\n"
    assert Workspace(ws).current_ref() == base
