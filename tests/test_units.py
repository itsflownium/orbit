from prbot.config import parse_config
from prbot.diffutil import added_lines, commentable_lines, glob_match
from prbot.llm import _parse_json, validate_review
from prbot.models import ChangedFile, PullRequest
from prbot.commenter import decode_state, encode_state, inline_body
from prbot.models import Finding
from prbot.commands import parse
from prbot.tools import parse_locations

from .fakes import pr_data

PATCH = """@@ -10,3 +10,4 @@ def f():
     a = 1
-    b = 2
+    b = 3
+    c = 4
     return a"""


def test_added_and_commentable_lines():
    assert added_lines(PATCH) == {11: "    b = 3", 12: "    c = 4"}
    assert commentable_lines(PATCH) == {10, 11, 12, 13}


def test_glob_match():
    assert glob_match("src/a/test_x.py", ["**/test_*.py"])
    assert glob_match("test_x.py", ["**/test_*.py"])
    assert glob_match("pkg/tests/helpers.py", ["**/tests/**"])
    assert not glob_match("src/x.py", ["**/tests/**"])
    assert glob_match("vendor/lib/x.go", ["vendor/**"])


def test_config_merges_and_keeps_defaults():
    cfg = parse_config("checks:\n  size:\n    max_lines: 5\nllm:\n  review_model: hermes3:8b\n")
    assert cfg.check("size")["max_lines"] == 5
    assert cfg.check("size")["max_files"] == 40  # default kept
    assert cfg["llm"]["review_model"] == "hermes3:8b"
    assert cfg["llm"]["provider"] == "auto"


def test_parse_json_strips_hermes_think_block():
    text = '<think>Let me check line 12 carefully... {"not": "this"}</think>\n{"findings": []}'
    assert _parse_json(text) == {"findings": []}
    assert _parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _parse_json("<think>unterminated reasoning {") is None
    assert _parse_json("no json here") is None


def _pr_with_patch():
    pr = PullRequest.from_api(pr_data())
    pr.files = [ChangedFile("src/m.py", "modified", 2, 1, PATCH)]
    return pr


def test_validate_review_drops_findings_outside_diff_and_low_confidence():
    pr = _pr_with_patch()
    rc = parse_config(None).check("llm_review")
    data = {"findings": [
        {"path": "src/m.py", "line": 11, "kind": "bug", "confidence": 0.9, "title": "b off by one",
         "explanation": "should be 2", "replacement": "    b = 2"},
        {"path": "src/m.py", "line": 99, "kind": "bug", "confidence": 0.99, "title": "hallucinated",
         "explanation": "", "replacement": ""},
        {"path": "other.py", "line": 11, "kind": "bug", "confidence": 0.99, "title": "wrong file",
         "explanation": "", "replacement": ""},
        {"path": "src/m.py", "line": 12, "kind": "bug", "confidence": 0.3, "title": "weak",
         "explanation": "", "replacement": ""},
        {"path": "src/m.py", "line": 12, "kind": "bug", "confidence": 0.95, "title": "multi",
         "explanation": "x", "replacement": "a\nb"},
    ]}
    out = validate_review(data, pr, pr.files, rc)
    assert [f.title for f in out] == ["bug: multi", "bug: b off by one"]
    assert out[0].suggestion is None  # multi-line replacement refused
    assert out[1].suggestion == "    b = 2"


def test_state_roundtrip_and_inline_body_fences():
    state = {"head_sha": "abc", "disputes": {"k": "--> tricky"}}
    assert decode_state(f"x <!-- prbot:state {encode_state(state)} --> y") == state
    body = inline_body(Finding(check="x", title="t", suggestion="print('```')", path="a", line=1))
    assert "````suggestion" in body


def test_command_parsing():
    cmds = parse("thanks!\n/bot dispute tests-missing docs only change\n/bot recheck")
    assert [(c.name, c.args) for c in cmds] == [("dispute", "tests-missing docs only change"), ("recheck", "")]


def test_tool_output_parsers():
    files = ["src/a.py", "src/b.py"]
    out = "src/a.py:3:1: F401 `os` imported but unused\nother.py:1:1: E1 x\nFound 2 errors."
    assert parse_locations(out, files) == [("src/a.py", 3, "F401 `os` imported but unused")]
    # eslint's unix formatter prints absolute paths
    assert parse_locations("/ws/src/b.py:9:2: no-undef [Error]", files, "/ws") == [("src/b.py", 9, "no-undef [Error]")]
