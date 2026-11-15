"""Regression tests for the second round of code reviews."""

import http.client
import io
import json
import os
import subprocess
import zipfile

import pytest
import yaml

from prbot import analyzer, commenter, llm, patches
from prbot import github as ghmod
from prbot.checks import Context
from prbot.commenter import decode_state
from prbot.config import parse_config
from prbot.github import GitHub, _retry_after
from prbot.models import ChangedFile, Finding, PullRequest
from prbot.runner import review_pr, sweep
from prbot.tools import TOOLS
from prbot.workspace import Workspace, _git_env

from .fakes import FakeGitHub, pr_data

ROOT = os.path.dirname(os.path.dirname(__file__))
CODE = "@@ -0,0 +1,12 @@\n" + "\n".join(f"+line_{i} = {i}" for i in range(1, 13))


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    for var in ("PRBOT_LLM_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "PRBOT_PUSH_TOKEN", "PRBOT_LOGIN"):
        monkeypatch.delenv(var, raising=False)


def make_gh(required=False, **kw):
    gh = FakeGitHub()
    if required:
        gh.repo_files[".github/prereview.yml"] = "analysis:\n  required: true\n"
    gh.prs[1] = pr_data(**kw)
    gh.files[1] = [ChangedFile("src/w.py", "added", 12, 0, CODE),
                   ChangedFile("tests/test_w.py", "added", 1, 0, "@@ -0,0 +1 @@\n+def test(): pass")]
    return gh


# ------------------------------------------------------------------ artifacts are found (H1) and picked safely

def _zip(data: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("analysis.json", json.dumps(data))
    return buf.getvalue()


def test_artifacts_envelope_is_paginated_and_candidates_are_filtered():
    gh = GitHub(token="t")
    pr = PullRequest.from_api(pr_data())
    sha = pr.head_sha
    runs = {
        1: {"event": "pull_request", "path": ".github/workflows/prereview-analyze.yml",
            "head_repository": {"full_name": "acme/widget"}, "head_branch": "feature"},
        2: {"event": "push", "path": ".github/workflows/prereview-analyze.yml",
            "head_repository": {"full_name": "acme/widget"}, "head_branch": "feature"},
        3: {"event": "pull_request", "path": ".github/workflows/other.yml",
            "head_repository": {"full_name": "acme/widget"}, "head_branch": "feature"},
        4: {"event": "pull_request", "path": ".github/workflows/prereview-analyze.yml",
            "head_repository": {"full_name": "evil/widget"}, "head_branch": "feature"},
    }
    arts = [{"id": i, "expired": False, "created_at": f"2026-01-0{i}", "workflow_run": {"id": i, "head_sha": sha},
             "archive_download_url": f"https://x/{i}", "size_in_bytes": 10} for i in runs]

    def request(method, path, *a, **k):
        if "/actions/artifacts" in path:
            return {"total_count": len(arts), "artifacts": arts}
        return runs[int(path.rsplit("/", 1)[1])]

    gh.request = request
    gh._last_link = ""
    found = gh.analysis_artifacts(pr.repo, "prbot-analysis", pr, ".github/workflows/prereview-analyze.yml")
    assert [a["id"] for a in found] == [1]


def test_every_candidate_is_tried_so_a_stray_artifact_cannot_hide_the_real_one():
    gh = FakeGitHub()
    pr = PullRequest.from_api(pr_data())
    pr.files = [ChangedFile("m.py", "modified", 1, 0, "@@ -1 +1,2 @@\n x\n+y = 1")]
    good = {"version": 1, "repo": pr.repo, "number": pr.number, "head_sha": pr.head_sha,
            "findings": [{"key": "lint-ruff", "title": "ruff says no", "severity": "blocking"}]}
    stray = dict(good, number=99)  # another PR at the same commit
    gh.artifacts[pr.head_sha] = [{"analysis.json": json.dumps(stray).encode()},
                                 {"analysis.json": json.dumps(good).encode()}]
    a = analyzer.fetch_analysis(gh, pr)
    assert a is not None and [f.key for f in a.findings] == ["lint-ruff"]


# ------------------------------------------------------------------ the patch filter cannot be bypassed (S1)

AUTHOR = {"m.py": {21: "x = 1", 22: "y = 2"}}


def _diff(body: str, path: str = "m.py") -> str:
    return f"diff --git a/{path} b/{path}\nindex 1..2 100644\n--- a/{path}\n+++ b/{path}\n{body}"


def test_hunk_header_that_lies_about_its_line_is_rejected():
    # header says line 21 (author's) but the '-' content is a maintainer line
    assert patches.keep_author_hunks(_diff("@@ -21,1 +21,1 @@\n-maint5\n+EVIL5\n"), AUTHOR) == []
    ok = patches.keep_author_hunks(_diff("@@ -21,1 +21,1 @@\n-x = 1\n+x = 10\n"), AUTHOR)
    assert [h.old_start for h in ok[0].hunks] == [21]


def test_smuggled_file_sections_and_extra_lines_are_rejected():
    smuggled = _diff("@@ -21,1 +21,1 @@\n-x = 1\n+x = 10\n--- a/victim.py\n+++ b/victim.py\n"
                     "@@ -1 +1 @@\n-safe\n+EVIL\n")
    assert patches.keep_author_hunks(smuggled, {**AUTHOR, "victim.py": {1: "safe"}}) == []
    extra = _diff("@@ -21,1 +21,1 @@\n-x = 1\n+x = 10\n+sneaky extra line\n")
    assert patches.keep_author_hunks(extra, AUTHOR) == []


def test_new_file_mode_and_binary_sections_are_rejected():
    new_file = "diff --git a/.gitattributes b/.gitattributes\nnew file mode 100644\n--- /dev/null\n" \
               "+++ b/.gitattributes\n@@ -0,0 +1 @@\n+* filter=evil\n"
    assert patches.parse(new_file) == []
    mode = "diff --git a/m.py b/m.py\nold mode 100644\nnew mode 100755\n--- a/m.py\n+++ b/m.py\n" \
           "@@ -21,1 +21,1 @@\n-x = 1\n+x = 10\n"
    assert patches.parse(mode) == []
    assert patches.parse(_diff("@@ -1 +1 @@\n-a\n+b\n", path="../escape.py")) == []
    assert patches.parse(_diff("@@ -1 +1 @@\n-a\n+b\n", path=".git/config")) == []


def test_hunk_spanning_maintainer_lines_is_rejected():
    wide = _diff("@@ -20,2 +20,2 @@\n-maint\n-x = 1\n+maint2\n+x = 10\n")
    assert patches.keep_author_hunks(wide, AUTHOR) == []


def test_render_writes_fresh_headers():
    kept = patches.keep_author_hunks(_diff("@@ -21,1 +21,1 @@\n-x = 1\n+x = 10\n"), AUTHOR)
    text = patches.render(kept)
    assert text.startswith("diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n@@ -21,1 +21,1 @@")
    assert "index" not in text


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _repo(tmp_path, files):
    repo = tmp_path / "r"
    repo.mkdir()
    for name, text in files.items():
        (repo / name).write_text(text)
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    return str(repo)


def test_apply_refuses_paths_outside_the_allowed_set(tmp_path):
    ws = Workspace(_repo(tmp_path, {"m.py": "x = 1\n", "victim.py": "safe\n"}))
    two = (_diff("@@ -1,1 +1,1 @@\n-x = 1\n+x = 2\n") + _diff("@@ -1,1 +1,1 @@\n-safe\n+EVIL\n", "victim.py"))
    assert not ws.apply(two, allowed={"m.py"})
    assert open(os.path.join(ws.path, "victim.py")).read() == "safe\n"
    assert ws.apply(_diff("@@ -1,1 +1,1 @@\n-x = 1\n+x = 2\n"), allowed={"m.py"})


def test_git_add_treats_paths_literally():
    assert _git_env()["GIT_LITERAL_PATHSPECS"] == "1"


def test_reset_removes_planted_files(tmp_path):
    ws = Workspace(_repo(tmp_path, {"m.py": "x = 1\n"}))
    keep = ws.untracked()
    with open(os.path.join(ws.path, ".gitattributes"), "w") as fh:
        fh.write("* text eol=crlf\n")
    ws.reset(keep_untracked=keep)
    assert not os.path.exists(os.path.join(ws.path, ".gitattributes"))


def test_insertion_and_final_newline_round_trip(tmp_path):
    ws = Workspace(_repo(tmp_path, {"m.py": "a\nb"}))  # no final newline
    with open(os.path.join(ws.path, "m.py"), "w") as fh:
        fh.write("import os\na\nb\n")
    diff = ws.diff()
    ws.reset()
    fps = patches.keep_author_hunks(diff, {"m.py": {1: "a", 2: "b"}})
    kinds = sorted((h.old_start, h.old_count) for fp in fps for h in fp.hunks)
    assert kinds == [(0, 0), (2, 1)]  # insertion at top + the final-newline fix (not a no-op)
    assert ws.apply(patches.render(fps), allowed={"m.py"})
    assert open(os.path.join(ws.path, "m.py")).read() == "import os\na\nb\n"


def test_top_of_file_insertion_becomes_a_suggestion(tmp_path):
    from prbot.fixer import suggestions
    ws = _repo(tmp_path, {"m.py": "a\n"})
    pr = PullRequest.from_api(pr_data(head_sha=subprocess.run(["git", "rev-parse", "HEAD"], cwd=ws,
                                                              capture_output=True, text=True).stdout.strip()))
    pr.files = [ChangedFile("m.py", "added", 1, 0, "@@ -0,0 +1 @@\n+a")]
    ctx = Context(pr=pr, gh=FakeGitHub(), cfg=parse_config(None), workspace=ws)
    fps = [patches.FilePatch("m.py", [patches.Hunk("m.py", 0, 0, [], ["import os"])])]
    out = suggestions(fps, ctx, ["ruff"])
    assert [(s.line, s.suggestion) for s in out] == [(1, "import os\na")]


# ------------------------------------------------------------------ the lint gate fails closed (S2)

def test_missing_analysis_keeps_the_pr_pending_until_it_arrives():
    gh = make_gh(required=True)
    res = review_pr(gh, "acme/widget", 1)
    assert not res["ready"] and gh.status_writes[-1]["state"] == "pending"
    assert "bot:ready-for-review" not in gh.labels.get(1, set())
    assert "waiting for lint results" in gh.summary(1)
    pr = gh.get_pr("acme/widget", 1)
    gh.artifacts[pr.head_sha] = [{"analysis.json": json.dumps(
        {"version": 1, "repo": pr.repo, "number": 1, "head_sha": pr.head_sha, "findings": []}).encode()}]
    res = review_pr(gh, "acme/widget", 1, force=True)  # the workflow_run event of the analysis job
    assert res["ready"] and gh.status_writes[-1]["state"] == "success"


# ------------------------------------------------------------------ untrusted keys (S4) and the bot's login (S3)

def test_artifact_keys_must_be_plain():
    pr = PullRequest.from_api(pr_data())
    a = analyzer.load_analysis({"version": 1, "repo": pr.repo, "number": 1, "head_sha": pr.head_sha,
                                "findings": [{"key": "lint-`</sub> @octocat <!--", "title": "t"},
                                             {"key": "lint-ruff", "title": "ok"}]}, pr)
    assert [f.key for f in a.findings] == ["lint-ruff"]
    body = commenter.render(pr, [Finding(check="x", title="t", key="k`</sub>@x<!--", severity="blocking")],
                            [], {}, {})
    assert "@x" not in body and "<!--" not in body.split("<!-- prbot:state")[0].replace(commenter.MARKER, "")


def test_action_passes_the_bot_login_through():
    action = yaml.safe_load(open(os.path.join(ROOT, "action.yml")))
    assert "bot-login" in action["inputs"]
    review = next(s for s in action["runs"]["steps"] if s.get("name") == "Run prbot")
    assert review["env"]["PRBOT_LOGIN"] == "${{ inputs.bot-login }}"


def test_login_prefers_the_configured_value(monkeypatch):
    monkeypatch.setenv("PRBOT_LOGIN", "my-app[bot]")
    assert GitHub(token="t").login() == "my-app[bot]"


# ------------------------------------------------------------------ concurrency config (M7 / S5)

def test_concurrency_is_job_level_so_plain_comments_cannot_cancel_runs():
    wf = yaml.safe_load(open(os.path.join(ROOT, "examples", "prereview-workflow.yml")))
    assert "concurrency" not in wf
    job = wf["jobs"]["prereview"]
    assert "concurrency" in job and "/bot" in job["if"]


# ------------------------------------------------------------------ model review timing (M4, M5)

class _Provider(llm.Provider):
    name = "fake"

    def __init__(self):
        self.calls = 0

    def complete_json(self, model, system, user, schema, max_tokens):
        self.calls += 1
        if "change_type" in json.dumps(schema):
            return {"change_type": "feature", "risk": "medium", "description_matches_diff": True, "mismatch_note": ""}
        return {"findings": []}


def _with_model(monkeypatch, gh, budget=None):
    provider = _Provider()
    monkeypatch.setattr(llm, "make_provider", lambda cfg: provider)
    if budget is not None:
        gh.repo_files[".github/prereview.yml"] += f"llm:\n  max_reviews_per_run: {budget}\n  review_model: m\n"
    else:
        gh.repo_files[".github/prereview.yml"] += "llm:\n  review_model: m\n"
    return provider


def test_model_review_skipped_for_budget_runs_on_the_next_pass(monkeypatch):
    gh = make_gh()
    p = _with_model(monkeypatch, gh, budget=0)
    review_pr(gh, "acme/widget", 1)
    assert p.calls == 0 and decode_state(gh.summary(1))["llm_pending"] is True
    gh.repo_files[".github/prereview.yml"] = gh.repo_files[".github/prereview.yml"].replace(
        "max_reviews_per_run: 0", "max_reviews_per_run: 5")
    res = review_pr(gh, "acme/widget", 1)  # not "unchanged": the review is still owed
    assert res.get("skipped") is None and p.calls == 2


def test_model_review_runs_once_the_only_blocker_is_disputed(monkeypatch):
    gh = make_gh(state="dirty", mergeable=False)
    p = _with_model(monkeypatch, gh)
    review_pr(gh, "acme/widget", 1)
    assert p.calls == 0
    review_pr(gh, "acme/widget", 1, force=True,
              mutate_state=lambda s: s.update(disputes={"mergeable-conflict": "rebased locally"}))
    assert p.calls == 2


# ------------------------------------------------------------------ LLM parsing (I2, I3)

def test_think_tags_inside_json_strings_survive():
    assert llm._parse_json('{"e": "strip the <think> tag"}') == {"e": "strip the <think> tag"}
    assert llm._parse_json('{"e": "a <think>x</think> b"}') == {"e": "a <think>x</think> b"}
    assert llm._parse_json('{"e": "ends with </think>"}') == {"e": "ends with </think>"}
    assert llm._parse_json('<think>reason {x}</think>\n{"a": 1}') == {"a": 1}
    assert llm._parse_json("<think>never closed {") is None


def test_fallback_response_reads_the_fallback_models_text():
    class B:
        def __init__(self, type, text=""):
            self.type, self.text = type, text

    class Resp:
        stop_reason = "end_turn"
        content = [B("text", '{"partial'), B("fallback"), B("text", '{"findings": []}')]

    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            return Resp()

    p = llm.AnthropicProvider.__new__(llm.AnthropicProvider)
    p.effort, p.use_fallbacks = "low", False

    class Client:
        class messages:
            @staticmethod
            def stream(**kw):
                return Stream()

    class A:
        class RateLimitError(Exception): ...
        class APIStatusError(Exception): ...
        class APIConnectionError(Exception): ...
        class BadRequestError(Exception): ...

    p.client, p.anthropic = Client(), A
    assert p.complete_json("anthropic-opus-5-5", "s", "u", {}, 100) == {"findings": []}


# ------------------------------------------------------------------ workspace paths through symlinks (I4)

def test_symlinked_workspace_still_lints(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "a.py").write_text("x=1\n")
    link = tmp_path / "link"
    os.symlink(real, link)
    pr = PullRequest.from_api(pr_data())
    pr.files = [ChangedFile("a.py", "modified", 1, 0, "@@ -1 +1 @@\n+x=1")]
    ctx = analyzer._Ctx(pr=pr, cfg=parse_config(None), workspace=str(link))
    ruff = next(t for t in TOOLS if t.name == "ruff")
    assert analyzer._files_for(ruff, ctx) == ["a.py"]
    assert Workspace(str(link)).path == os.path.realpath(str(link))


# ------------------------------------------------------------------ comment size (M6)

def test_hundreds_of_secrets_make_one_finding_and_a_comment_under_the_limit():
    gh = make_gh()
    body = "@@ -0,0 +1,600 @@\n" + "\n".join(f"+k{i} = 'AKIA{i:016d}'" for i in range(600))
    gh.files[1] = [ChangedFile("fixtures/keys.py", "added", 600, 0, body)]
    res = review_pr(gh, "acme/widget", 1)
    secret = [f for f in res["findings"] if f.key.startswith("debris-secret")]
    assert len(secret) == 1
    many = [Finding(check="x", title="t" * 200, key=f"k{i}", severity="blocking", detail="d" * 2000) for i in range(500)]
    out = commenter.render(gh.get_pr("acme/widget", 1), many, ["n"] * 100, {}, {})
    assert len(out) <= commenter.MAX_BODY and decode_state(out) == {}


# ------------------------------------------------------------------ transport and permissions (I5, I6, I11)

def test_dropped_connection_is_retried_for_reads(monkeypatch):
    calls = {"n": 0}

    class Resp:
        headers = {"Link": ""}

        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def open_(req, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise http.client.RemoteDisconnected("gone")
        return Resp()

    monkeypatch.setattr(ghmod._OPENER, "open", open_)
    monkeypatch.setattr(ghmod.time, "sleep", lambda s: None)
    assert GitHub(token="t").request("GET", "/x") == {"ok": True} and calls["n"] == 2


def test_custom_roles_with_write_count_as_maintainers():
    gh = GitHub(token="t")
    gh.request = lambda *a, **k: {"permission": "write", "role_name": "contributor-plus"}
    assert gh.permission("a/b", "u") == "write"
    gh.request = lambda *a, **k: {"permission": "read", "role_name": "triage"}
    assert gh.permission("a/b", "u") == "triage"


def test_retry_after_is_capped():
    assert _retry_after("100000", 1) == 300


def test_ruff_hook_with_comment_or_quotes_counts_as_enforced():
    import re
    ruff = next(t for t in TOOLS if t.name == "ruff")
    assert re.search(ruff.enforce, "      - id: ruff  # lint", re.M)
    assert re.search(ruff.enforce, '      - id: "ruff"', re.M)
    assert not re.search(ruff.enforce, "      - id: ruff-format", re.M)


# ------------------------------------------------------------------ state edge cases (L8, L9, L10)

def test_paused_pr_is_not_rewritten_on_every_sweep():
    gh = make_gh()
    review_pr(gh, "acme/widget", 1, mutate_state=lambda s: s.update(paused=True))
    n = len(gh.status_writes)
    sweep(gh, "acme/widget")
    sweep(gh, "acme/widget")
    assert len(gh.status_writes) == n


def test_local_analysis_only_runs_for_prs_that_are_reviewed():
    gh = make_gh()
    review_pr(gh, "acme/widget", 1)
    calls = []
    sweep(gh, "acme/widget", analyze_fn=lambda pr, cfg: calls.append(pr.number))
    assert calls == []  # unchanged PR: no fetch, no linters


def test_deleted_summary_comment_is_recreated():
    gh = make_gh()
    review_pr(gh, "acme/widget", 1)
    pr = gh.get_pr("acme/widget", 1)
    commenter.upsert_summary(gh, pr, 999999, commenter.MARKER + "\nx")
    assert any(c["body"].endswith("\nx") for c in gh.comments[1])


def test_analyze_refuses_a_checkout_with_saved_credentials(tmp_path):
    ws = _repo(tmp_path, {"m.py": "x\n"})
    _git(ws, "config", "--local", "http.https://github.com/.extraheader", "AUTHORIZATION: basic abc")
    pr = PullRequest.from_api(pr_data())
    with pytest.raises(analyzer.WorkspaceDirty):
        analyzer.analyze(pr, parse_config(None), ws)


def test_bot_fix_without_a_patch_says_so():
    gh = make_gh()
    pr = gh.get_pr("acme/widget", 1)
    gh.artifacts[pr.head_sha] = [{"analysis.json": json.dumps(
        {"version": 1, "repo": pr.repo, "number": 1, "head_sha": pr.head_sha,
         "findings": [{"key": "lint-ruff", "title": "ruff", "severity": "blocking"}], "patch": ""}).encode()}]
    gh.create_comment("acme/widget", 1, "/bot fix", login="alice")
    review_pr(gh, "acme/widget", 1)
    assert "no fix patch is available" in gh.summary(1)
