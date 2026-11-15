"""Automatic fixes, applied without running any PR code.

  - Branch behind base with no conflicts: GitHub's update-branch API merges base in.
  - Lint/format issues: the analysis job already ran the repo's fixers and produced a patch.
    That patch is re-filtered to hunks touching lines the PR added, then either
      commit  - applied with `git apply` and pushed to the PR branch. Needs PRBOT_PUSH_TOKEN (a
                GitHub App or fine-grained token) so the push triggers CI; fork PRs also need
                "Allow edits by maintainers".
      suggest - posted as GitHub suggestion blocks the author applies in one click.
    If commit is not possible, the bot falls back to suggest.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from . import patches
from .checks import Context
from .diffutil import commentable_lines
from .models import ADVISORY, Finding
from .workspace import Workspace

log = logging.getLogger(__name__)


@dataclass
class FixResult:
    pushed: bool = False  # head moved; a fresh review will follow
    resolved: set[str] = field(default_factory=set)  # finding keys fixed by a commit
    suggestions: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def push_token() -> str | None:
    return os.environ.get("PRBOT_PUSH_TOKEN")


def can_push(ctx: Context) -> bool:
    if not push_token() or not ctx.pr.head_repo:
        return False
    return not ctx.pr.is_fork or ctx.pr.maintainer_can_modify


def apply_fixes(ctx: Context, findings: list[Finding], state: dict, force_commit: bool = False) -> FixResult:
    res = FixResult()
    af = ctx.cfg["autofix"]
    if not af.get("enabled") or af.get("mode") == "off":
        return res
    keys = {f.key for f in findings}
    mode = "commit" if force_commit else af.get("mode", "suggest")

    # 1. Branch behind base with no conflicts: GitHub can merge base in for us.
    if "mergeable-behind" in keys and af.get("update_branch") and can_push(ctx) and not ctx.gh.dry_run:
        from .github import GitHub
        if GitHub(token=push_token()).update_branch(ctx.pr.repo, ctx.pr.number, ctx.pr.head_sha):
            res.pushed = True
            res.resolved.add("mergeable-behind")
            res.notes.append(f"Merged the latest `{ctx.pr.base_ref}` into this branch.")
            return res

    # 2. Lint/format fixes produced by the analysis job.
    lint_keys = {f.key for f in findings if f.check == "lint"}
    analysis = ctx.analysis
    if not lint_keys or analysis is None or not analysis.file_patches:
        return res
    fps = analysis.file_patches
    tool_names = sorted({k[len("lint-"):] for k in lint_keys})
    # one push attempt per commit (unless asked again with /bot fix); suggestions are
    # rebuilt every time and de-duplicated when posted
    if mode == "commit" and state.get("autofix_sha") == ctx.pr.head_sha and not force_commit:
        mode = "suggest"
    if mode == "commit":
        state["autofix_sha"] = ctx.pr.head_sha

    if mode == "commit" and can_push(ctx) and not ctx.gh.dry_run and ctx.workspace:
        ws = Workspace(ctx.workspace, token=ctx.gh.token)
        restore = ws.head()
        keep = ws.untracked()
        paths = {fp.path for fp in fps}
        try:
            if ws.checkout_pr(ctx.pr) and ws.apply(patches.render(fps), allowed=paths):
                msg = f"{af['commit_message']}\n\nApplied: {', '.join(tool_names)}"
                if ws.commit_and_push(ctx.pr, sorted(paths), msg, push_token()):
                    res.pushed = True
                    res.resolved |= lint_keys
                    res.notes.append(f"Pushed a commit applying {', '.join(f'`{t}`' for t in tool_names)} "
                                     f"to {len(fps)} file(s), on lines this PR added.")
                    return res
        finally:
            ws.reset(keep_untracked=keep)  # nothing a patch planted survives into later work
            ws.checkout(restore)
        res.notes.append("Could not push fixes to this branch; posting them as suggestions instead.")
    elif mode == "commit" and not ctx.gh.dry_run:
        if ctx.pr.is_fork and not ctx.pr.maintainer_can_modify:
            why = "the fork does not allow maintainer edits"
        elif not push_token():
            why = "no push token is configured"
        else:
            why = "no checkout is available to commit from"
        res.notes.append(f"Fixes are posted as suggestions because {why}.")

    res.suggestions = suggestions(fps, ctx, tool_names)
    return res


def suggestions(fps: list[patches.FilePatch], ctx: Context, tools_used: list[str]) -> list[Finding]:
    """GitHub suggestion findings for fixer hunks that GitHub lets us comment on."""
    by_path = {f.filename: f.patch for f in ctx.files}
    label = ", ".join(f"`{t}`" for t in tools_used)
    out: list[Finding] = []
    for fp in fps:
        allowed = commentable_lines(by_path.get(fp.path))
        for h in fp.hunks:
            if h.is_noop():
                continue
            new = [ln.rstrip("\r") for ln in h.new_lines]
            if h.old_count == 0:
                # Pure insertion: GitHub can only replace lines, so anchor on a neighbour and repeat it.
                if h.old_start == 0:  # at the top of the file: before line 1
                    if 1 not in allowed:
                        continue
                    original = _line_at(ctx, fp.path, 1)
                    if original is None:
                        continue
                    first = last = 1
                    body = [*new, original]
                else:
                    if h.old_start not in allowed:
                        continue
                    original = _line_at(ctx, fp.path, h.old_start)
                    if original is None:
                        continue
                    first = last = h.old_start
                    body = [original, *new]
            else:
                first, last = h.old_start, h.old_start + h.old_count - 1
                if any(n not in allowed for n in range(first, last + 1)):
                    continue
                body = new
            out.append(Finding(
                check="autofix", severity=ADVISORY, path=fp.path, line=last,
                start_line=first if first != last else None,
                title=f"Suggested fix from {label}", detail="", suggestion="\n".join(body),
                key=f"autofix-{fp.path}-{first}-{last}",
            ))
    return out


def _line_at(ctx: Context, path: str, line_no: int) -> str | None:
    text = None
    if ctx.workspace:
        text = Workspace(ctx.workspace).show(ctx.pr.head_sha, path)
    if text is None:
        try:
            text = ctx.gh.get_file_text(ctx.pr.repo, path, ctx.pr.head_sha)
        except Exception:  # missing file, API error
            text = None
    if text is None:
        return None
    lines = text.split("\n")
    return lines[line_no - 1].rstrip("\r") if 0 < line_no <= len(lines) else None
