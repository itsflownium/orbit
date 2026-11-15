"""Unified-diff parsing and path globbing helpers.

GitHub's per-file `patch` field has no file headers, so every body line is classified by
its first character only. A deleted line `-- comment` is the patch line `--- comment`
and must not be mistaken for a header.
"""

from __future__ import annotations

import re
from functools import lru_cache

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def patch_lines(patch: str) -> list[str]:
    """Split on '\\n' only. str.splitlines() also splits on form feeds, U+2028 and friends,
    which can appear inside a source line and would shift every later line number."""
    lines = patch.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _walk(patch: str | None):
    """Yield (kind, new_line_no, text) for every hunk body line. kind is '+', '-' or ' '."""
    if not patch:
        return
    line_no = 0
    in_hunk = False
    for raw in patch_lines(patch):
        m = _HUNK.match(raw)
        if m:
            line_no = int(m.group(1))
            in_hunk = True
            continue
        if not in_hunk:
            continue
        kind = raw[:1]
        if kind == "\\":  # "\ No newline at end of file"
            continue
        if kind == "-":
            yield "-", None, raw[1:]
        elif kind == "+":
            yield "+", line_no, raw[1:]
            line_no += 1
        else:  # context (' ') or an empty context line
            yield " ", line_no, raw[1:]
            line_no += 1


def added_lines(patch: str | None) -> dict[int, str]:
    """Map new-file line number -> content (without a trailing CR) for every added line."""
    return {n: text.rstrip("\r") for kind, n, text in _walk(patch) if kind == "+"}


def commentable_lines(patch: str | None) -> set[int]:
    """Right-side line numbers GitHub accepts for review comments (added + context)."""
    return {n for kind, n, _ in _walk(patch) if kind != "-"}


@lru_cache(maxsize=512)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    i, n, out = 0, len(pattern), ""
    while i < n:
        c = pattern[i]
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif c == "*":
            out += "[^/]*"
            i += 1
        elif c == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(c)
            i += 1
    return re.compile(f"^{out}$")


def glob_match(path: str, patterns: list[str] | tuple[str, ...]) -> bool:
    return any(_glob_regex(p).match(path) for p in patterns)


def top_dir(path: str) -> str:
    return path.split("/", 1)[0] if "/" in path else "(root)"
