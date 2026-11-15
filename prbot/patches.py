"""Zero-context diff hunks produced by running fixers, filtered to the PR author's own lines.

Patch text from the analysis job is untrusted, so it is never handed to `git apply` as-is:
  - parsing is strict: a hunk must contain exactly the number of '-' and '+' lines its header
    declares, '-' lines before '+' lines; anything else (smuggled file headers, extra lines,
    mode/rename/binary sections) makes that file's patch invalid and it is dropped
  - every '-' line must be a line the PR added, at that line number, with that exact content
    (checked against the GitHub API's view of the PR, not the artifact)
  - render() writes fresh headers for each file, so nothing from the original headers survives

The diff text comes from `git -c core.quotePath=false diff -U0`, decoded with surrogateescape
so CRLF line endings and non-UTF-8 bytes survive the round trip.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_ESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "a": "\a", "b": "\b", "f": "\f", "r": "\r", "v": "\v"}
_PLAIN_PATH = re.compile(r"^[A-Za-z0-9._/+@=,-]+$")
_ALLOWED_FILE_HEADERS = ("diff --git ", "index ", "--- ", "+++ ")


def _unquote(path: str) -> str:
    """Undo git's C-style quoting ("b/caf\\303\\251.py") in case quotePath was on."""
    if not (len(path) >= 2 and path[0] == '"' and path[-1] == '"'):
        return path
    body, out, i = path[1:-1], bytearray(), 0
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in "01234567":
                out.append(int(body[i + 1:i + 4], 8) & 0xFF)
                i += 4
                continue
            out.extend(_ESCAPES.get(nxt, nxt).encode())
            i += 2
            continue
        out.extend(c.encode("utf-8", "surrogateescape"))
        i += 1
    return out.decode("utf-8", "surrogateescape")


def _quote(path: str) -> str:
    """Quote a path the way git does, so `git apply` reads it back exactly."""
    if _PLAIN_PATH.match(path):
        return path
    out = ['"']
    for byte in path.encode("utf-8", "surrogateescape"):
        ch = chr(byte)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\n":
            out.append("\\n")
        elif 0x20 <= byte < 0x7F:
            out.append(ch)
        else:
            out.append(f"\\{byte:03o}")
    out.append('"')
    return "".join(out)


def safe_path(path: str) -> bool:
    parts = path.split("/")
    return bool(path) and not path.startswith("/") and ".." not in parts and ".git" not in parts \
        and "" not in parts and not any(ch in path for ch in ("\0", "\n", "\r"))


def _new_path(header_line: str) -> str | None:
    """Path from a '+++ b/path' line; None for '+++ /dev/null'."""
    rest = header_line[4:].rstrip("\r")
    if rest.endswith("\t"):  # git appends a tab to paths containing spaces
        rest = rest[:-1]
    rest = _unquote(rest)
    if rest == "/dev/null":
        return None
    return rest[2:] if rest.startswith("b/") else rest


@dataclass
class Hunk:
    path: str
    old_start: int
    old_count: int
    old_lines: list[str] = field(default_factory=list)  # without the leading '-', CRs kept
    new_lines: list[str] = field(default_factory=list)  # without the leading '+', CRs kept
    old_no_eol: bool = False  # "\ No newline at end of file" after the old side
    new_no_eol: bool = False

    def old_range(self) -> range:
        if self.old_count == 0:  # pure insertion after old_start
            return range(self.old_start, self.old_start + 2)
        return range(self.old_start, self.old_start + self.old_count)

    def is_noop(self) -> bool:
        """True when the only change is line endings (e.g. a formatter turning CRLF into LF).
        Adding or removing a final newline is a real change."""
        if self.old_no_eol != self.new_no_eol:
            return False
        return [s.rstrip("\r") for s in self.old_lines] == [s.rstrip("\r") for s in self.new_lines]

    def by_author(self, added: dict[int, str]) -> bool:
        """Every line this hunk rewrites is one the PR added, with exactly that content.
        Insertions must sit next to a line the PR added."""
        if self.old_count == 0:
            return any(n in added for n in (self.old_start, self.old_start + 1)) or \
                (self.old_start == 0 and 1 in added)
        for offset, text in enumerate(self.old_lines):
            n = self.old_start + offset
            if n not in added or added[n] != text.rstrip("\r"):
                return False
        return True

    def body(self) -> list[str]:
        out = [f"@@ -{self.old_start},{self.old_count} +{self.old_start},{len(self.new_lines)} @@"]
        out += ["-" + ln for ln in self.old_lines]
        if self.old_no_eol:
            out.append("\\ No newline at end of file")
        out += ["+" + ln for ln in self.new_lines]
        if self.new_no_eol:
            out.append("\\ No newline at end of file")
        return out


@dataclass
class FilePatch:
    path: str
    hunks: list[Hunk] = field(default_factory=list)


def parse(diff: str) -> list[FilePatch]:
    """Strict parse of a `git diff -U0` text. Malformed file sections are dropped."""
    lines = diff.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    files: list[FilePatch] = []
    i, n = 0, len(lines)
    while i < n:
        if not lines[i].startswith("diff --git "):
            i += 1
            continue
        # header block
        i += 1
        path, ok = None, True
        while i < n and not lines[i].startswith(("@@", "diff --git ")):
            line = lines[i]
            if not line.startswith(_ALLOWED_FILE_HEADERS):
                ok = False  # mode change, rename, copy, binary, similarity...
            if line.startswith("+++ "):
                path = _new_path(line)
            i += 1
        fp = FilePatch(path or "")
        # hunks
        while i < n and lines[i].startswith("@@"):
            h = _HUNK.match(lines[i])
            if not h:
                ok = False
                i += 1
                break
            old_count = int(h.group(2)) if h.group(2) is not None else 1
            new_count = int(h.group(4)) if h.group(4) is not None else 1
            hunk = Hunk(fp.path, int(h.group(1)), old_count)
            i += 1
            while i < n and lines[i].startswith("-") and len(hunk.old_lines) < old_count:
                hunk.old_lines.append(lines[i][1:])
                i += 1
            if i < n and lines[i].startswith("\\") and len(hunk.old_lines) == old_count and old_count:
                hunk.old_no_eol = True
                i += 1
            while i < n and lines[i].startswith("+") and len(hunk.new_lines) < new_count:
                hunk.new_lines.append(lines[i][1:])
                i += 1
            if i < n and lines[i].startswith("\\") and len(hunk.new_lines) == new_count and new_count:
                hunk.new_no_eol = True
                i += 1
            if len(hunk.old_lines) != old_count or len(hunk.new_lines) != new_count:
                ok = False
            fp.hunks.append(hunk)
        # anything after the hunks that is not the next file is a smuggling attempt
        if i < n and not lines[i].startswith("diff --git "):
            ok = False
            while i < n and not lines[i].startswith("diff --git "):
                i += 1
        if ok and path and safe_path(path) and fp.hunks:
            files.append(fp)
    return files


def keep_author_hunks(diff: str, added: dict[str, dict[int, str]] | dict[str, set[int]]) -> list[FilePatch]:
    """Only hunks that rewrite lines the PR added; never code that was already there.

    `added` maps path -> {line: content} (preferred, content is checked) or path -> {line}.
    """
    out = []
    for fp in parse(diff):
        lines = added.get(fp.path)
        if not lines:
            continue
        content = lines if isinstance(lines, dict) else None
        kept = []
        for h in fp.hunks:
            if h.is_noop():
                continue
            if content is not None:
                ok = h.by_author(content)
            else:
                ok = (h.old_count == 0 and any(x in lines for x in h.old_range())) or \
                     (h.old_count > 0 and all(x in lines for x in h.old_range()))
            if ok:
                kept.append(h)
        if kept:
            out.append(FilePatch(fp.path, kept))
    return out


def render(patches: list[FilePatch]) -> str:
    """A patch `git apply --unidiff-zero` accepts, with headers written from scratch."""
    out = []
    for fp in patches:
        if not safe_path(fp.path):
            continue
        q = _quote(fp.path)
        a = _quote("a/" + fp.path) if q.startswith('"') else "a/" + fp.path
        b = _quote("b/" + fp.path) if q.startswith('"') else "b/" + fp.path
        out += [f"diff --git {a} {b}", f"--- {a}", f"+++ {b}"]
        for h in fp.hunks:
            out.extend(h.body())
    return "\n".join(out) + "\n" if out else ""


def paths_in(patch_text: str) -> set[str]:
    return {fp.path for fp in parse(patch_text)}
