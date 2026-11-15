"""Keep model citations anchored to source the reviewer actually received."""

from prbot import commenter, llm
from prbot.config import parse_config
from prbot.models import ChangedFile

from .fakes import FakeGitHub, pr_data


REPO = "acme/widget"


def test_deep_review_links_related_source_at_the_reviewed_commit(monkeypatch):
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.example")
    gh = FakeGitHub()
    gh.prs[1] = pr_data()
    gh.files[1] = [ChangedFile("api.py", "modified", 1, 1, "@@ -1,2 +1,2 @@\n def make():\n-    return []\n+    return None")]
    cfg = parse_config(
        "checks:\n  llm_review:\n    context_paths: ['caller [api].py', ignored/other.py]\n"
        "ignore_paths: ['ignored/**']\n"
    )
    pr = gh.get_pr(REPO, 1)
    reads = []

    def get_source(repo, path, ref):
        reads.append((repo, path, ref))
        return {
            "caller [api].py": "import api\nfor item in api.make():\n    print(item)\n",
            "api.py": "def make():\n    return None\n",
        }.get(path)

    monkeypatch.setattr(gh, "get_file_text", get_source)

    class Reviewer:
        def complete_json(self, model, system, user, schema, max_tokens):
            assert 'File "caller [api].py"\n1 | import api\n2 | for item in api.make():' in user
            return {"findings": [{
                "path": "api.py", "line": 2, "kind": "bug", "confidence": 0.99,
                "title": "Iteration now raises", "explanation": "The caller iterates over the return value.",
                "recommendation": "Keep returning an iterable.",
                "references": [
                    {"path": "caller [api].py", "start_line": 2, "end_line": 3},
                    {"path": "caller [api].py", "start_line": 3, "end_line": 4},
                    {"path": "missing.py", "start_line": 1, "end_line": 1},
                    {"path": "api.py", "start_line": False, "end_line": 2},
                ],
            }]}

    findings, reason = llm.deep_review(Reviewer(), cfg, pr, pr.files, None, gh=gh)
    assert reason is None and len(findings) == 1
    assert reads == [(REPO, path, pr.head_sha) for path in ("caller [api].py", "api.py")]
    link = (f"[caller \\[api\\].py:2–3](https://github.example/{REPO}/blob/{pr.head_sha}"
            "/caller%20%5Bapi%5D.py#L2-L3)")
    assert findings[0].references == [link]
    assert link in commenter.inline_body(findings[0])
    assert link in commenter.render(pr, findings, [], {}, cfg)


def test_diff_can_cite_unchanged_context_without_turning_it_into_an_inline_anchor():
    gh = FakeGitHub()
    gh.prs[1] = pr_data()
    gh.files[1] = [ChangedFile("m.py", "modified", 1, 1, "@@ -5,2 +5,2 @@\n unchanged\n-old\n+new")]
    pr = gh.get_pr(REPO, 1)
    items = [
        {"path": "m.py", "line": 5, "confidence": 1},
        {"path": "m.py", "line": 6, "confidence": 1,
         "references": [{"path": "m.py", "start_line": 5, "end_line": 5}]},
    ]
    findings = llm.validate_review({"findings": items}, pr, pr.files, parse_config(None).check("llm_review"))
    assert [f.line for f in findings] == [6]
    assert findings[0].references[0].endswith("/m.py#L5)")
