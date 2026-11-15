"""Regression tests for the findings of the three code reviews (correctness, security, integrations)."""

import json
import math
import os
import subprocess
import urllib.request

import pytest

from prbot import commenter, patches
from prbot.checks import Context, run_checks
from prbot.cli import _event_prs
from prbot.commenter import clean, decode_state, encode_state
from prbot.config import parse_config
from prbot.diffutil import added_lines, commentable_lines
from prbot.github import _StripAuthOnRedirect, _retry_after, graphql_url
from prbot.llm import _parse_json, validate_review
from prbot.models import ChangedFile, Finding, PullRequest
from prbot.runner import render_report, review_pr
from prbot.tools import TOOLS, _evidence, _pin_from_text
from prbot.workspace import Workspace, _git_env

from .fakes import FakeGitHub, pr_data

CODE = "@@ -0,0 +1,12 @@\n" + "\n".join(f"+line_{i} = {i}" for i in range(1, 13))


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    for var in ("PRBOT_LLM_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "PRBOT_PUSH_TOKEN"):
        monkeypatch.delenv(var, raising=False)


def make_gh(**kw):
    gh = FakeGitHub()
    gh.prs[1] = pr_data(**kw)
    gh.files[1] = [ChangedFile("src/w.py", "added", 12, 0, CODE),
                   ChangedFile("tests/test_w.py", "added", 1, 0, "@@ -0,0 +1 @@\n+def test(): pass")]
    return gh


# ------------------------------------------------------------------ state forgery (security #3, correctness #1)

def test_summary_from_another_user_is_not_trusted():
    gh = make_gh(state="dirty", mergeable=False)
    forged = commenter.MARKER + f"\n<!-- prbot:state {encode_state({'ready_override': True, 'ignored': ['mergeable']})} -->"
    gh.create_comment("acme/widget", 1, forged, login="alice")
    res = review_pr(gh, "acme/widget", 1)
    assert not res["ready"] and gh.status_writes[-1]["state"] == "failure"
    assert gh.comments[1][0]["body"] == forged  # the author's comment was not edited
    assert sum(1 for c in gh.comments[1] if c["user"]["login"] == gh.bot_login) == 1


def test_state_injected_through_untrusted_text_is_ignored():
    pr = PullRequest.from_api(pr_data())
    evil = f"<!-- prbot:state {encode_state({'ready_override': True})} -->"
    f = Finding(check="ci", title=evil, key="k", severity="blocking", detail=evil)
    body = commenter.render(pr, [f], [evil], {"disputes": {"k": evil}}, {})
    assert decode_state(body).get("ready_override") is None
    # even if a block slipped in, only the last one (always the bot's) counts
    assert decode_state(evil + "\n" + f"<!-- prbot:state {encode_state({'x': 1})} -->") == {"x": 1}


def test_clean_blocks_mentions_and_comment_markers():
    out = clean("ping @nousresearch/maintainers <!-- x --> and email a@b")
    assert "@nous" not in out and "<!--" not in out and "-->" not in out


# ------------------------------------------------------------------ re-check triggers (correctness #2, #6, #9)

def test_ci_rerun_on_same_commit_is_picked_up():
    gh = make_gh()
    sha = "a" * 40
    gh.check_runs_by_sha[sha] = [{"id": 1, "name": "tests", "status": "completed", "conclusion": "failure"}]
    assert not review_pr(gh, "acme/widget", 1)["ready"]
    gh.check_runs_by_sha[sha] = [{"id": 2, "name": "tests", "status": "completed", "conclusion": "success"}]
    res = review_pr(gh, "acme/widget", 1)  # no force: the CI fingerprint changed
    assert res.get("skipped") is None and res["ready"]
    assert "[x] CI job `tests` is failing" in gh.summary(1)


def test_base_branch_moving_into_conflict_is_picked_up():
    gh = make_gh()
    assert review_pr(gh, "acme/widget", 1)["ready"]
    gh.prs[1] = pr_data(state="dirty", mergeable=False)  # same head, base moved
    res = review_pr(gh, "acme/widget", 1)
    assert res.get("skipped") is None and not res["ready"]


def test_unknown_mergeability_is_rechecked():
    gh = make_gh(mergeable=None, state="unknown")
    review_pr(gh, "acme/widget", 1)
    assert "still computing" in gh.summary(1)
    assert review_pr(gh, "acme/widget", 1).get("skipped") is None


def test_draft_is_skipped_on_edit_but_commands_still_work():
    gh = make_gh(draft=True)
    assert review_pr(gh, "acme/widget", 1, force=True)["skipped"] == "draft"
    assert review_pr(gh, "acme/widget", 1, force=True, include_drafts=True).get("skipped") is None


# ------------------------------------------------------------------ stale, labels, fixed list (correctness #3, #8, #11)

def test_stale_keeps_duplicate_label_and_is_saved(monkeypatch):
    import prbot.runner as runner
    gh = make_gh(state="dirty", mergeable=False)
    gh.prs[2] = pr_data(number=2, title="Add widget frobnication too", author="bob", head_sha="d" * 40)
    gh.files[2] = [ChangedFile("src/w.py", "added", 1, 0, "@@ -0,0 +1 @@\n+x")]
    review_pr(gh, "acme/widget", 1)
    assert gh.labels[1] == {"bot:needs-changes", "bot:possible-duplicate"}
    monkeypatch.setattr(runner, "_now", lambda: 10**12)
    review_pr(gh, "acme/widget", 1)
    assert gh.labels[1] == {"bot:needs-changes", "bot:possible-duplicate", "bot:stale"}
    assert decode_state(gh.summary(1))["stale"] is True
    assert review_pr(gh, "acme/widget", 1)["skipped"] == "unchanged"  # saved, so not redone


def test_paused_pr_keeps_paused_label_in_sweeps(monkeypatch):
    import prbot.runner as runner
    gh = make_gh(state="dirty", mergeable=False)
    review_pr(gh, "acme/widget", 1, mutate_state=lambda s: s.update(paused=True))
    monkeypatch.setattr(runner, "_now", lambda: 10**12)
    review_pr(gh, "acme/widget", 1)
    assert gh.labels[1] == {"bot:paused"}


def test_ignored_check_is_not_listed_as_fixed():
    gh = make_gh(state="dirty", mergeable=False)
    review_pr(gh, "acme/widget", 1)
    review_pr(gh, "acme/widget", 1, force=True, mutate_state=lambda s: s.update(ignored=["mergeable"]))
    assert "Fixed since last check" not in gh.summary(1)


def test_disputed_label_only_for_live_findings():
    gh = make_gh()
    review_pr(gh, "acme/widget", 1, mutate_state=lambda s: s.update(disputes={"gone-key": "typo"}))
    assert "bot:disputed" not in gh.labels[1] and "bot:ready-for-review" in gh.labels[1]


def test_inline_dedupe_trims_oldest_not_alphabetical():
    gh = make_gh()
    pr = gh.get_pr("acme/widget", 1)
    state = {"posted_inline": [f"z{i}" for i in range(200)]}
    commenter.post_inline(gh, pr, [Finding(check="x", title="t", key="a-new", path="src/w.py", line=1)], state, 5)
    assert state["posted_inline"][-1] == "a-new" and "z0" not in state["posted_inline"]


# ------------------------------------------------------------------ concurrency (correctness #7)

def test_concurrent_command_change_restarts_the_review():
    gh = make_gh(state="dirty", mergeable=False)
    review_pr(gh, "acme/widget", 1)
    pr = gh.get_pr("acme/widget", 1)
    real = gh.issue_comments
    calls = {"n": 0}

    def racing(repo, number):
        calls["n"] += 1
        if calls["n"] == 2:  # another run's /bot ready lands while this run is working
            cid, st = commenter.find_summary_from(gh, real(repo, number))
            st["ready_override"] = True
            gh.update_comment(repo, cid, commenter.render(pr, [], [], st, parse_config(None)))
        return real(repo, number)

    gh.issue_comments = racing
    res = review_pr(gh, "acme/widget", 1, force=True)
    state = decode_state(gh.summary(1))
    # one consistent outcome: the override is kept, and the comment, status and labels agree
    assert state["ready_override"] is True and res["ready"]
    assert gh.status_writes[-1]["state"] == "success"
    assert "bot:ready-for-review" in gh.labels[1] and "bot:needs-changes" not in gh.labels[1]
    assert "A maintainer marked this PR ready" in gh.summary(1)


def test_command_from_a_cancelled_run_is_applied_later():
    gh = make_gh(state="dirty", mergeable=False)
    gh.permissions["maint"] = "write"
    review_pr(gh, "acme/widget", 1)
    gh.create_comment("acme/widget", 1, "/bot ignore mergeable", login="maint")  # its run was cancelled
    res = review_pr(gh, "acme/widget", 1)  # e.g. the next push or sweep
    assert res["ready"] and decode_state(gh.summary(1))["ignored"] == ["mergeable"]
    assert review_pr(gh, "acme/widget", 1)["skipped"] == "unchanged"  # applied once, not again


# ------------------------------------------------------------------ config + report (correctness #12, #13)

def test_config_booleans_and_nulls():
    cfg = parse_config("checks:\n  lint: false\n  size: null\n  llm_review: false\nllm: null\n")
    assert cfg.check("lint")["enabled"] is False and cfg.check("lint")["severity"] == "blocking"
    assert cfg.check("size")["max_lines"] == 800
    assert cfg["llm"]["provider"] == "auto"
    with pytest.raises(ValueError):
        parse_config("checks:\n  size: 3\n")


def test_linked_issue_in_html_comment_does_not_count():
    gh = FakeGitHub()
    gh.prs[1] = pr_data(body="A long enough description of the change. <!-- Fixes #123 -->")
    cfg = parse_config("checks:\n  description:\n    require_linked_issue: true\n")
    keys = [f.key for f in run_checks(Context(pr=gh.get_pr("acme/widget", 1), gh=gh, cfg=cfg), names=["description"])]
    assert keys == ["description-no-issue"]


def test_report_counts_unchanged_prs_in_their_real_bucket():
    results = [{"number": 1, "title": "a", "author": "x", "skipped": "unchanged", "ready": True, "blocking": 0},
               {"number": 2, "title": "b", "author": "y", "skipped": "unchanged", "ready": False, "blocking": 1,
                "blocking_titles": ["CI failing"]},
               {"number": 3, "title": "c", "author": "z", "skipped": "draft"}]
    report = render_report("acme/widget", results)
    assert "| Ready for maintainer review | 1 |" in report
    assert "| Needs changes from the author | 1 |" in report
    assert "| Skipped (draft, paused, bot author) | 1 |" in report
    assert "CI failing" in report


def test_workflow_run_prs_are_filtered_to_this_repo():
    gh = FakeGitHub()
    run = {"head_sha": "a" * 40, "pull_requests": [
        {"number": 7, "base": {"repo": {"name": "widget", "url": "https://api.github.com/repos/acme/widget"}}},
        {"number": 9, "base": {"repo": {"name": "widget", "url": "https://api.github.com/repos/other/widget"}}}]}
    assert _event_prs(gh, "acme/widget", run) == [7]


# ------------------------------------------------------------------ LLM output (integrations #5, #6; correctness #5)

def test_parse_json_with_code_block_inside_a_string_and_stray_think_close():
    inner = json.dumps({"findings": [{"explanation": "use:\n```py\nx = 1\n```"}]})
    assert _parse_json(inner)["findings"][0]["explanation"].startswith("use:")
    assert _parse_json("```json\n" + inner + "\n```") is not None
    assert _parse_json('reasoning {maybe}</think>{"a": 1}') == {"a": 1}


def _review_pr():
    pr = PullRequest.from_api(pr_data())
    pr.files = [ChangedFile("m.py", "modified", 2, 0, "@@ -1 +1,3 @@\n x = 1\n+y = 2\n+z = 3")]
    return pr


def test_validate_review_survives_bad_types_and_nan():
    pr = _review_pr()
    rc = parse_config(None).check("llm_review")
    data = {"findings": [
        {"path": "m.py", "line": 2, "confidence": 0.9, "replacement": ["y"], "kind": "bug", "title": "t"},
        {"path": "m.py", "line": 2, "confidence": float("nan"), "title": "nan"},
        {"path": ["m.py"], "line": 2, "confidence": 0.9},
        "not a dict",
    ]}
    out = validate_review(data, pr, pr.files, rc)
    assert len(out) == 1 and out[0].suggestion is None
    assert validate_review({"findings": "nope"}, pr, pr.files, rc) == []
    assert not math.isnan(0.9)


def test_llm_finding_keys_are_stable_across_rewording():
    pr = _review_pr()
    rc = parse_config(None).check("llm_review")
    a = validate_review({"findings": [{"path": "m.py", "line": 2, "kind": "bug", "confidence": 0.9,
                                       "title": "y is wrong", "explanation": ""}]}, pr, pr.files, rc)
    b = validate_review({"findings": [{"path": "m.py", "line": 2, "kind": "bug", "confidence": 0.95,
                                       "title": "Incorrect value assigned to y", "explanation": ""}]}, pr, pr.files, rc)
    assert a[0].key == b[0].key and a[0].key.startswith("llm-")


# ------------------------------------------------------------------ diff parsing (integrations #1, #10)

def test_deleted_line_starting_with_dashes_does_not_shift_numbers():
    patch = "@@ -1,3 +1,3 @@\n--- old comment\n+-- new comment\n x = 1\n+y = 2\n+++i;"
    assert sorted(added_lines(patch)) == [1, 3, 4]
    assert added_lines(patch)[4] == "++i;"
    assert sorted(commentable_lines(patch)) == [1, 2, 3, 4]


def test_form_feed_inside_a_line_does_not_split_it():
    patch = "@@ -0,0 +1,3 @@\n+a\x0cb\n+c\n+d"
    assert added_lines(patch) == {1: "a\x0cb", 2: "c", 3: "d"}


def test_crlf_added_lines_drop_the_cr():
    assert added_lines("@@ -0,0 +1 @@\n+x = 1\r") == {1: "x = 1"}


# ------------------------------------------------------------------ git round trips (integrations #3, #4)

def _repo(tmp_path, files: dict[str, bytes]):
    repo = tmp_path / "r"
    repo.mkdir()
    for name, data in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_bytes(data)
    for cmd in (["git", "init", "-q"], ["git", "-c", "core.autocrlf=false", "add", "."],
                ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=repo, check=True)
    return str(repo)


def test_crlf_and_latin1_files_round_trip_through_diff_and_apply(tmp_path):
    ws_path = _repo(tmp_path, {"a.py": b"x=1\r\ny=2\r\n", "l.py": "s = 'caf\xe9'\n".encode("latin-1")})
    ws = Workspace(ws_path)
    with open(os.path.join(ws_path, "a.py"), "wb") as fh:
        fh.write(b"x = 1\r\ny=2\r\n")
    with open(os.path.join(ws_path, "l.py"), "wb") as fh:
        fh.write("s = 'caf\xe9!'\n".encode("latin-1"))
    diff = ws.diff()
    ws.reset()
    assert ws.apply(diff), "git apply must accept the CRLF/latin-1 patch"
    assert open(os.path.join(ws_path, "a.py"), "rb").read() == b"x = 1\r\ny=2\r\n"


def test_line_ending_only_changes_are_not_suggested():
    diff = "diff --git a/a.js b/a.js\n--- a/a.js\n+++ b/a.js\n@@ -1 +1 @@\n-x=1\r\n+x=1\n"
    assert patches.keep_author_hunks(diff, {"a.js": {1}}) == []


def test_paths_with_spaces_and_unicode_are_parsed(tmp_path):
    ws_path = _repo(tmp_path, {"my dir/a b.py": b"x=1\n", "café.py": b"y=1\n"})
    for name in ("my dir/a b.py", "café.py"):
        with open(os.path.join(ws_path, name), "w") as fh:
            fh.write("changed=1\n")
    paths = {fp.path for fp in patches.parse(Workspace(ws_path).diff())}
    assert paths == {"my dir/a b.py", "café.py"}
    quoted = '+++ "b/caf\\303\\251.py"'
    assert patches._new_path(quoted) == "café.py"


# ------------------------------------------------------------------ tools (integrations #11, #12; security #7, #8)

def _tool(name):
    return next(t for t in TOOLS if t.name == name)


def test_enforcement_patterns_cover_common_forms(tmp_path):
    import re
    assert re.search(_tool("ruff").enforce, "      - id: ruff-check", re.M)
    assert re.search(_tool("ruff").enforce, "uses: astral-sh/ruff-action@v3", re.M)
    assert re.search(_tool("prettier").enforce, "run: npx prettier . --check", re.M)
    ws = tmp_path / "w"
    ws.mkdir()
    (ws / "package.json").write_text(json.dumps({"devDependencies": {"eslint": "9"}}))
    assert not re.search(_tool("eslint").enforce, _evidence(str(ws)))  # a dependency is not enforcement
    (ws / "package.json").write_text(json.dumps({"devDependencies": {"eslint": "9"}, "scripts": {"lint": "eslint ."}}))
    assert re.search(_tool("eslint").enforce, _evidence(str(ws)))


def test_pin_formats():
    assert _pin_from_text('dev = ["black[jupyter]==24.1.0"]', "black") == "24.1.0"
    assert _pin_from_text('[tool.poetry.group.dev.dependencies]\nruff = "0.4.1"\n', "ruff") == "0.4.1"
    assert _pin_from_text('ruff = "==0.4.2"', "ruff") == "0.4.2"
    assert _pin_from_text('ruff = "^0.4"', "ruff") is None  # a range is not a pin
    assert _pin_from_text("pyruff==1.0", "ruff") is None


def test_rustfmt_is_not_run_on_forks_and_files_follow_double_dash():
    assert _tool("rustfmt").safe_untrusted is False
    assert "--" in _tool("ruff").check_args(["x.py"])


def test_option_like_and_symlinked_files_are_skipped(tmp_path):
    from prbot.analyzer import _Ctx, _files_for
    ws = tmp_path / "w"
    ws.mkdir()
    (ws / "ok.py").write_text("x=1\n")
    (ws / "-rf.py").write_text("x=1\n")
    os.symlink("/etc/hosts", ws / "link.py")
    pr = PullRequest.from_api(pr_data())
    pr.files = [ChangedFile(n, "modified", 1, 0, "@@ -1 +1 @@\n+x") for n in ("ok.py", "-rf.py", "link.py")]
    ctx = _Ctx(pr=pr, cfg=parse_config(None), workspace=str(ws))
    assert _files_for(_tool("ruff"), ctx) == ["ok.py"]


# ------------------------------------------------------------------ transport and credentials (security #4, #10; integrations #8, #13)

def test_credentials_go_through_env_not_argv():
    env = _git_env("secret-token")
    assert env["GIT_CONFIG_KEY_0"] == "http.extraheader" and "secret-token" not in json.dumps(
        [v for k, v in env.items() if k != "GIT_CONFIG_VALUE_0"])
    assert "GITHUB_ENV" not in env and env["UV_NO_CONFIG"] == "1"


def test_redirect_to_another_host_drops_authorization():
    req = urllib.request.Request("https://api.github.com/repos/a/b/actions/jobs/1/logs",
                                 headers={"Authorization": "Bearer t"})
    new = _StripAuthOnRedirect().redirect_request(req, None, 302, "Found", {}, "https://blob.example.net/log?sig=1")
    assert "Authorization" not in new.headers and "authorization" not in {k.lower() for k in new.headers}
    same = _StripAuthOnRedirect().redirect_request(req, None, 302, "Found", {}, "https://api.github.com/other")
    assert same.headers.get("Authorization") == "Bearer t"


def test_graphql_url_on_enterprise(monkeypatch):
    monkeypatch.delenv("GITHUB_GRAPHQL_URL", raising=False)
    assert graphql_url("https://ghe.example.com/api/v3") == "https://ghe.example.com/api/graphql"
    assert graphql_url("https://api.github.com") == "https://api.github.com/graphql"


def test_retry_after_accepts_http_dates():
    assert _retry_after("7", 1) == 7
    assert _retry_after("Wed, 21 Oct 2015 07:28:00 GMT", 3) == 0  # in the past
    assert _retry_after("garbage", 3) == 3
