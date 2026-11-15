"""Exercise the repository's fix policy through the normal review entry point."""

from unittest.mock import Mock

import pytest

from prbot import analyzer, fixer, llm
from prbot.github import GitHub
from prbot.models import ChangedFile
from prbot.runner import review_pr

from .fakes import FakeGitHub, pr_data


REPO = "acme/widget"
PATH = "src/stats.py"
RECOMMENDATION = "Return zero for an empty list and add a regression test with no records."


def _pr(config, state="clean"):
    gh = FakeGitHub()
    gh.prs[1] = pr_data(state=state)
    gh.files[1] = [ChangedFile(
        PATH, "added", 5, 0,
        "@@ -0,0 +1,5 @@\n+import math\n+\n+\n+def mean_size(values):\n+    return sum(values) / len(values)",
    )]
    gh.repo_files[".github/prereview.yml"] = config
    pr = gh.get_pr(REPO, 1)
    result = {
        "version": analyzer.VERSION,
        "repo": REPO,
        "number": pr.number,
        "head_sha": pr.head_sha,
        "base_sha": pr.base_sha,
        "base_ref": pr.base_ref,
        "findings": [{
            "key": "lint-ruff",
            "title": "Unused import",
            "detail": "Remove the unused `math` import on line 1.",
            "severity": "blocking",
        }],
        "patch": (
            "diff --git a/src/stats.py b/src/stats.py\n"
            "--- a/src/stats.py\n"
            "+++ b/src/stats.py\n"
            "@@ -1 +0,0 @@\n"
            "-import math\n"
        ),
    }
    return gh, result


def _inline(gh):
    return [comment for review in gh.reviews for comment in review["comments"]]


class _Reviewer:
    def complete_json(self, model, system, user, schema, max_tokens):
        if "change_type" in schema["properties"]:
            return {"change_type": "feature", "risk": "medium", "description_matches_diff": True, "mismatch_note": ""}
        return {"findings": [{
            "path": PATH,
            "line": 5,
            "kind": "bug",
            "confidence": 0.95,
            "title": "Mean calculation fails for an empty list",
            "explanation": "An empty input raises ZeroDivisionError.",
            "recommendation": RECOMMENDATION,
            "replacement": "    return sum(values) / len(values) if values else 0",
        }]}


@pytest.mark.parametrize("setting", ["mode: comment", "mode: 'off'", "mode: off", "enabled: false"])
def test_comment_only_reports_both_findings_without_editing_the_branch(monkeypatch, tmp_path, setting):
    gh, analysis = _pr(f"""autofix:
  {setting}
  update_branch: true
llm:
  provider: hermes
checks:
  llm_review:
    only_when_clean: false
""", state="behind")
    monkeypatch.setenv("PRBOT_PUSH_TOKEN", "fixture")
    monkeypatch.setattr(llm, "make_provider", lambda cfg: _Reviewer())
    update = Mock()
    checkout = Mock()
    monkeypatch.setattr(GitHub, "update_branch", update)
    monkeypatch.setattr(fixer, "Workspace", checkout)

    result = review_pr(gh, REPO, 1, analysis=analysis, workspace=str(tmp_path), force_fix=True)

    assert result["status"] == "failure"
    assert "Remove the unused `math` import" in gh.summary(1)
    assert RECOMMENDATION in gh.summary(1)
    assert "This repository uses comment-only mode." in gh.summary(1)
    model_comment, = _inline(gh)
    assert model_comment["path"] == PATH and model_comment["line"] == 5
    assert "An empty input raises ZeroDivisionError." in model_comment["body"]
    assert "How to fix" in model_comment["body"] and RECOMMENDATION in model_comment["body"]
    assert "return sum(values) / len(values) if values else 0" in model_comment["body"]
    assert "```suggestion" not in model_comment["body"]
    checkout.assert_not_called()
    update.assert_not_called()


@pytest.mark.parametrize("mode", ["suggest", "commit"])
def test_suggestions_are_clickable_when_the_author_can_apply_them(monkeypatch, mode):
    gh, analysis = _pr(f"autofix:\n  mode: {mode}\nllm:\n  provider: none\n")
    monkeypatch.delenv("PRBOT_PUSH_TOKEN", raising=False)

    result = review_pr(gh, REPO, 1, analysis=analysis)

    assert result["status"] == "failure"
    suggestion, = _inline(gh)
    assert suggestion["line"] == 1 and suggestion["path"] == PATH
    assert "```suggestion\n```" in suggestion["body"]


def test_summary_does_not_claim_a_suggestion_github_rejected():
    gh, analysis = _pr("autofix:\n  mode: suggest\nllm:\n  provider: none\n")
    gh.reject_lines.add((PATH, 1))

    result = review_pr(gh, REPO, 1, analysis=analysis)

    assert result["status"] == "failure"
    assert _inline(gh) == []
    assert "Remove the unused `math` import" in gh.summary(1)
    assert "inline suggestion(s) posted" not in gh.summary(1)


def test_suggest_mode_does_not_update_a_behind_branch(monkeypatch, tmp_path):
    gh, analysis = _pr("autofix:\n  mode: suggest\n  update_branch: true\nllm:\n  provider: none\n",
                       state="behind")
    monkeypatch.setenv("PRBOT_PUSH_TOKEN", "fixture")
    update = Mock()
    checkout = Mock()
    monkeypatch.setattr(GitHub, "update_branch", update)
    monkeypatch.setattr(fixer, "Workspace", checkout)

    result = review_pr(gh, REPO, 1, analysis=analysis, workspace=str(tmp_path))

    assert result["status"] == "failure"
    assert "```suggestion" in _inline(gh)[0]["body"]
    checkout.assert_not_called()
    update.assert_not_called()


def test_commit_mode_passes_only_the_deterministic_lint_patch_to_the_fixer(monkeypatch, tmp_path):
    gh, analysis = _pr("autofix:\n  mode: commit\nllm:\n  provider: none\n")
    monkeypatch.setenv("PRBOT_PUSH_TOKEN", "fixture")
    checkout = Mock()
    checkout.current_ref.return_value = "main"
    checkout.git.return_value = Mock(code=0, out="")
    checkout.has_credentials.return_value = False
    checkout.untracked.return_value = set()
    checkout.checkout_pr.return_value = True
    checkout.apply.return_value = True
    checkout.commit_and_push.return_value = True
    monkeypatch.setattr(fixer, "Workspace", Mock(return_value=checkout))

    result = review_pr(gh, REPO, 1, analysis=analysis, workspace=str(tmp_path))

    assert result["pushed"] is True
    checkout.apply.assert_called_once()
    assert "-import math" in checkout.apply.call_args.args[0]
    assert checkout.apply.call_args.kwargs["allowed"] == {PATH}
    checkout.commit_and_push.assert_called_once()
    assert checkout.commit_and_push.call_args.args[1] == [PATH]
    assert _inline(gh) == []
