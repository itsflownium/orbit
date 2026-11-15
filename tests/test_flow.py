"""End-to-end runs of the bot against the fake GitHub, and a fake Hermes server."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

from prbot import cli, llm
from prbot.commands import handle_comment
from prbot.models import ChangedFile
from prbot.runner import render_report, review_pr, sweep

from .fakes import FakeGitHub, pr_data

CODE_PATCH = "@@ -0,0 +1,12 @@\n" + "\n".join(f"+line_{i} = {i}" for i in range(1, 13))


@pytest.fixture(autouse=True)
def no_llm_env(monkeypatch):
    for var in ("PRBOT_LLM_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "PRBOT_PUSH_TOKEN"):
        monkeypatch.delenv(var, raising=False)


def make_gh(**pr_kwargs):
    gh = FakeGitHub()
    gh.prs[1] = pr_data(**pr_kwargs)
    gh.files[1] = [ChangedFile("src/widget.py", "added", 12, 0, CODE_PATCH)]
    return gh


def test_blocking_issues_comment_label_and_status():
    gh = make_gh(state="dirty", mergeable=False)
    gh.check_runs_by_sha["a" * 40] = [{"id": 7, "name": "tests", "status": "completed", "conclusion": "failure",
                                       "details_url": "https://ci/7", "app": {"slug": "github-actions"},
                                       "output": {"summary": "1 failed"}}]
    res = review_pr(gh, "acme/widget", 1)
    assert res["blocking"] == 2 and not res["ready"]
    body = gh.summary(1)
    assert "2 items to fix" in body
    assert "CI job `tests` is failing" in body and "assert 1 == 2" in body
    assert "Merge conflicts with `main`" in body
    assert "no tests were added" in body  # advisory by default
    assert gh.labels[1] == {"bot:needs-changes"}
    assert gh.status_writes[-1]["state"] == "failure"


def test_rerun_unchanged_is_skipped_and_fix_is_ticked_off():
    gh = make_gh(state="dirty", mergeable=False)
    review_pr(gh, "acme/widget", 1)
    assert review_pr(gh, "acme/widget", 1)["skipped"] == "unchanged"
    # author pushes a rebase that fixes the conflict and adds tests
    gh.prs[1] = pr_data(head_sha="c" * 40)
    gh.files[1].append(ChangedFile("tests/test_widget.py", "added", 5, 0, "@@ -0,0 +1 @@\n+def test(): pass"))
    res = review_pr(gh, "acme/widget", 1)
    assert res["ready"]
    body = gh.summary(1)
    assert "Pre-review passed" in body and "[x] Merge conflicts" in body
    assert len([c for c in gh.comments[1]]) == 1  # edited in place, not reposted
    assert gh.labels[1] == {"bot:ready-for-review"}
    assert gh.status_writes[-1]["state"] == "success"


def test_secret_is_blocking_with_inline_comment():
    gh = make_gh()
    gh.files[1] = [ChangedFile("app/config.py", "modified", 1, 0,
                               "@@ -1,1 +1,2 @@\n import os\n+KEY = 'AKIAABCDEFGHIJKLMNOP'")]
    res = review_pr(gh, "acme/widget", 1)
    assert res["blocking"] == 1
    assert "Possible AWS access key" in gh.summary(1)
    assert gh.reviews and gh.reviews[0]["comments"][0]["line"] == 2
    assert "AKIAABCDEFGHIJKLMNOP" not in gh.summary(1)  # never echo the secret


def test_dispute_and_maintainer_commands():
    gh = make_gh(state="dirty", mergeable=False)
    review_pr(gh, "acme/widget", 1)

    def comment(body, login="alice", assoc="CONTRIBUTOR"):
        c = gh.create_comment("acme/widget", 1, body, login=login)  # the command lives on the PR
        return {"action": "created", "repository": {"full_name": "acme/widget"},
                "issue": {"number": 1, "pull_request": {}, "user": {"login": "alice"}},
                "comment": {**c, "author_association": assoc},
                "sender": {"type": "User"}}

    handle_comment(gh, comment("/bot dispute mergeable-conflict GitHub UI is wrong"))
    body = gh.summary(1)
    assert "Disputed" in body and "GitHub UI is wrong" in body
    # a disputed blocker no longer blocks, but the PR is not "ready" until a maintainer decides
    assert gh.labels[1] == {"bot:disputed"} and gh.status_writes[-1]["state"] == "pending"
    assert "waiting for a maintainer" in body

    # a non-maintainer cannot pause
    handle_comment(gh, comment("/bot pause", login="mallory"))
    assert "for maintainers" in gh.comments[1][-1]["body"]

    # org membership is not enough: write permission is checked through the API
    handle_comment(gh, comment("/bot pause", login="orgmember", assoc="MEMBER"))
    assert "for maintainers" in gh.comments[1][-1]["body"]
    gh.permissions["maint"] = "write"
    handle_comment(gh, comment("/bot pause", login="maint", assoc="MEMBER"))
    assert "Pre-review paused" in gh.summary(1)
    assert gh.labels[1] == {"bot:paused"}
    handle_comment(gh, comment("/bot resume", login="maint", assoc="MEMBER"))
    assert "paused" not in gh.summary(1).split("\n")[1].lower()


def test_overlap_and_sweep_report():
    gh = make_gh()
    gh.prs[2] = pr_data(number=2, title="Add widget frobnication support", author="bob", head_sha="d" * 40)
    gh.files[2] = [ChangedFile("src/widget.py", "added", 3, 0, "@@ -0,0 +1 @@\n+x = 1")]
    results = sweep(gh, "acme/widget")
    assert "May duplicate 1 other open PR" in gh.summary(1)
    assert "bot:possible-duplicate" in gh.labels[1]
    report = render_report("acme/widget", results)
    assert "2 open PRs scanned" in report


# ---------------------------------------------------------------- Hermes provider


class _HermesHandler(BaseHTTPRequestHandler):
    calls: list = []
    reject_json_mode = True

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).calls.append(body)
        if "response_format" in body and type(self).reject_json_mode:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error": "response_format not supported"}')
            return
        system = body["messages"][0]["content"]
        if '"change_type"' in system:
            answer = {"change_type": "feature", "risk": "medium", "description_matches_diff": True, "mismatch_note": ""}
        else:
            answer = {"findings": [{"path": "src/widget.py", "line": 3, "kind": "bug", "confidence": 0.9,
                                    "title": "wrong constant", "explanation": "line_3 should be 30",
                                    "replacement": "line_3 = 30"}]}
        content = "<think>checking the diff</think>\n" + json.dumps(answer)
        out = json.dumps({"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


@pytest.fixture
def hermes_server(monkeypatch):
    _HermesHandler.calls = []
    server = HTTPServer(("127.0.0.1", 0), _HermesHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("PRBOT_LLM_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
    yield _HermesHandler
    server.shutdown()


def test_hermes_review_end_to_end(hermes_server, monkeypatch):
    gh = make_gh()
    gh.files[1].append(ChangedFile("tests/test_widget.py", "added", 1, 0, "@@ -0,0 +1 @@\n+def test(): pass"))
    res = review_pr(gh, "acme/widget", 1)
    assert res["ready"]  # model findings are advisory by default
    calls = hermes_server.calls
    models = [c["model"] for c in calls]
    assert "NousResearch/Hermes-4-14B" in models and "NousResearch/Hermes-4-70B" in models
    assert "response_format" in calls[0] and all("response_format" not in c for c in calls[1:])
    assert "<schema>" in calls[-1]["messages"][0]["content"]
    assert "wrong constant" in gh.summary(1)
    inline = gh.reviews[-1]["comments"][0]
    assert inline["path"] == "src/widget.py" and inline["line"] == 3
    assert "```suggestion\nline_3 = 30\n```" in inline["body"]
    # same commit again: cached, no new model calls
    n = len(calls)
    review_pr(gh, "acme/widget", 1, force=True)
    assert len(hermes_server.calls) == n
    review_pr(gh, "acme/widget", 1, fresh_model=True)
    assert len(hermes_server.calls) == n * 2
    assert len(gh.reviews) == 1  # still de-duplicates inline comments
    monkeypatch.setattr(llm, "REVIEW_CACHE_VERSION", llm.REVIEW_CACHE_VERSION + 1)
    review_pr(gh, "acme/widget", 1)
    assert len(hermes_server.calls) == n * 3


def test_schedule_retries_a_failed_fresh_model_review(hermes_server, monkeypatch):
    gh = make_gh()
    repo = "acme/widget"
    gh.repo_files[".github/prereview.yml"] += "llm: {provider: hermes}\nchecks: {llm_review: {severity: blocking}}\n"
    assert review_pr(gh, repo, 1)["status"] == "failure"  # model finds the wrong constant
    n = len(hermes_server.calls)

    with monkeypatch.context() as outage:
        outage.delenv("PRBOT_LLM_BASE_URL")
        result = review_pr(gh, repo, 1, fresh_model=True)
        assert result["status"] == "pending" and result["waiting_on"] == ["model review"]
        assert len(hermes_server.calls) == n

    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.setenv("GITHUB_REPOSITORY", repo)
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert cli.cmd_event(SimpleNamespace(workspace=None, verbose=False, dry_run=False), gh) == 0
    assert len(hermes_server.calls) == n * 2
    assert gh.status_writes[-1]["state"] == "failure" and "wrong constant" in gh.summary(1)


def test_model_review_waits_for_blocking_issues(hermes_server):
    gh = make_gh(state="dirty", mergeable=False)
    review_pr(gh, "acme/widget", 1)
    assert hermes_server.calls == []
    assert "runs after the blocking issues are fixed" in gh.summary(1)


def test_model_review_waits_for_pending_required_ci(hermes_server):
    gh = make_gh()
    sha = gh.get_pr("acme/widget", 1).head_sha
    gh.check_runs_by_sha[sha] = [{"name": "test", "status": "in_progress"}]
    assert review_pr(gh, "acme/widget", 1)["status"] == "pending"
    assert not hermes_server.calls
    gh.check_runs_by_sha[sha][0].update(status="completed", conclusion="success")
    assert review_pr(gh, "acme/widget", 1)["status"] == "success"
    assert hermes_server.calls


