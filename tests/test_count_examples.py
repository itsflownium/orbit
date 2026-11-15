"""Exercise verified counts through the same findings Orbit posts to GitHub."""

import pytest

from prbot import commenter, llm
from prbot.config import parse_config
from prbot.models import ChangedFile

from .fakes import FakeGitHub, pr_data


SOURCE = '''def count_between(values: list[int], lower: int, upper: int) -> int:
    """Count values between lower and upper, including both bounds."""
    return sum(lower <= value < upper for value in values)
'''
REPLACEMENT = "    return sum(lower <= value <= upper for value in values)"
CALL = "count_between([0, 1, 2, 2, 3], 1, 2)"


def posted_finding(description, replacement=REPLACEMENT, source=SOURCE):
    gh = FakeGitHub()
    gh.prs[1] = pr_data()
    patch = f"@@ -0,0 +1,{len(source.splitlines())} @@\n" + "\n".join(
        f"+{line}" for line in source.splitlines()
    )
    gh.files[1] = [ChangedFile("counts.py", "added", len(source.splitlines()), 0, patch)]
    pr = gh.get_pr("acme/widget", 1)
    pr.body = description
    config = parse_config(None).check("llm_review")
    model = {"findings": [{
        "path": "counts.py", "line": len(source.splitlines()), "kind": "bug", "confidence": 0.99,
        "title": "The example counts 2",
        "explanation": "The sample currently returns 2.",
        "recommendation": "Change the comparison to make it return 2 instead.",
        "replacement": replacement,
    }]}
    finding, = llm.validate_review(model, pr, pr.files, config)
    return commenter.inline_body(finding, allow_suggestions=False)


@pytest.mark.parametrize(
    "call, actual, expected",
    [(CALL, 1, 3), ("count_between([-3, -1, -1, 2], -1, -1)", 0, 2)],
)
def test_comment_replaces_model_arithmetic_with_a_static_count(call, actual, expected):
    body = posted_finding(
        f"For example, `{call}` should return `{expected}`.", REPLACEMENT.lstrip() + "  # the count is 2"
    )
    assert f"For `{call}`, the current comparison counts **{actual}**." in body
    assert f"Including both bounds counts **{expected}**." in body
    assert "The example counts 2" not in body
    assert "currently returns 2" not in body
    assert "make it return 2" not in body
    assert "# the count is 2" not in body
    assert REPLACEMENT in body


@pytest.mark.parametrize(
    "call, expected, replacement",
    [
        ("count_between([read_input(), 1, 2], 1, 2)", 3, REPLACEMENT),
        ("count_between([False, 1, 2], 0, 2)", 3, REPLACEMENT),
        (CALL, 4, REPLACEMENT),
        (CALL, 3, REPLACEMENT + " + side_effect()"),
    ],
)
def test_comment_does_not_claim_verification_for_unsupported_examples(call, expected, replacement):
    body = posted_finding(f"`{call}` should return `{expected}`.", replacement)
    assert "the current comparison counts" not in body
    assert "Including both bounds counts" not in body


def test_count_respects_parameter_order_and_can_check_an_exclusive_lower_bound():
    source = """def count_between(lower, values, upper):
    return sum(lower < value <= upper for value in values)
"""
    call = "count_between(-1, [-2, -1, -1, 3, 4], 3)"
    body = posted_finding(f"`{call}` should return `3`.", source=source)
    assert "values equal to the lower bound" in body
    assert "the current comparison counts **1**" in body
    assert "Including both bounds counts **3**" in body
