"""`/bot ...` commands posted as PR comments."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

MAINTAINER_PERMISSIONS = {"admin", "maintain", "write"}

log = logging.getLogger(__name__)

_CMD = re.compile(r"^\s*/bot\s+(\w+)(?:\s+(.*))?$", re.M)

HELP = """**prbot commands**

- `/bot recheck` — run the checks again
- `/bot fix` — apply automatic fixes (formatters/linters, update branch)
- `/bot dispute <key> <reason>` — mark a finding as wrong; it stops blocking and a maintainer decides
- `/bot help` — this message

Maintainers only:
- `/bot ready` — mark the PR ready for review regardless of findings
- `/bot unready` — undo `/bot ready`
- `/bot pause` / `/bot resume` — stop or restart automated review on this PR
- `/bot ignore <check>` / `/bot unignore <check>` — turn a check off for this PR
  (checks: ci, mergeable, size, tests, description, debris, overlap, lint, llm_review)
- `/bot resolve <key>` — accept a disputed finding as wrong and stop showing it"""


@dataclass
class Command:
    name: str
    args: str


def parse(body: str) -> list[Command]:
    return [Command(m.group(1).lower(), (m.group(2) or "").strip()) for m in _CMD.finditer(body or "")]


@dataclass
class Pending:
    """Commands found in PR comments that no earlier run has applied yet."""
    mutations: list
    replies: list[str]
    force_fix: bool
    comment_ids: list[int]
    last_id: int


def collect(gh, repo: str, pr_author: str, comments: list[dict], state: dict) -> Pending:
    """Every `/bot` comment newer than the last one applied. Reading them from the PR (not only
    from the triggering event) means a cancelled or skipped workflow run never loses a command."""
    from .commenter import is_bot_comment

    seen = int(state.get("cmd_seen") or 0)
    mutations: list = []
    replies: list[str] = []
    force_fix = False
    ids: list[int] = []
    last = seen
    perms: dict[str, bool] = {}

    def maintainer(actor: str) -> bool:
        # author_association MEMBER/COLLABORATOR includes people without write access
        if actor not in perms:
            perms[actor] = bool(actor) and gh.permission(repo, actor) in MAINTAINER_PERMISSIONS
        return perms[actor]

    for c in sorted(comments, key=lambda c: c.get("id") or 0):
        cid = c.get("id") or 0
        if cid <= seen:
            continue
        user = c.get("user") or {}
        if user.get("type") == "Bot" or is_bot_comment(gh, c):
            continue
        cmds = parse(c.get("body") or "")
        if not cmds:
            continue
        last = max(last, cid)
        ids.append(cid)
        actor = user.get("login", "")
        is_author = bool(actor) and actor == pr_author
        for cmd in cmds:
            if cmd.name == "help":
                replies.append(HELP)
            elif cmd.name == "recheck":
                pass
            elif cmd.name == "fix":
                if is_author or maintainer(actor):
                    force_fix = True
                else:
                    replies.append(f"@{actor} only the PR author or a maintainer can run `/bot fix`.")
            elif cmd.name == "dispute":
                if not (is_author or maintainer(actor)):
                    replies.append(f"@{actor} only the PR author or a maintainer can dispute findings.")
                    continue
                key, _, reason = cmd.args.partition(" ")
                if not key:
                    replies.append("Usage: `/bot dispute <key> <reason>` (the key is shown next to each finding).")
                    continue
                reason = reason.strip() or "no reason given"
                mutations.append(lambda s, k=key[:64], r=reason, a=actor:
                                 s.setdefault("disputes", {}).update({k: f"{a}: {r}"[:300]}))
            elif cmd.name in ("ready", "unready", "pause", "resume", "ignore", "unignore", "resolve"):
                if not maintainer(actor):
                    replies.append(f"@{actor} `/bot {cmd.name}` is for maintainers with write access.")
                    continue
                mutations.append(_maintainer_mutation(cmd))
            else:
                replies.append(f"Unknown command `/bot {cmd.name}`. Try `/bot help`.")
    return Pending(mutations, replies, force_fix, ids, last)


def handle_comment(gh, event: dict, workspace: str | None = None) -> dict:
    """issue_comment event: re-review the PR; review_pr applies every pending command."""
    from .runner import review_pr

    comment = event.get("comment") or {}
    issue = event.get("issue") or {}
    repo = (event.get("repository") or {}).get("full_name")
    if "pull_request" not in issue or not repo:
        return {"skipped": "not a pull request"}
    sender = event.get("sender") or {}
    if sender.get("type") == "Bot" or (comment.get("user") or {}).get("type") == "Bot":
        return {"skipped": "comment from a bot"}
    cmds = parse(comment.get("body", ""))
    if not cmds:
        return {"skipped": "no command"}
    result = review_pr(gh, repo, issue["number"], workspace=workspace, force=True, include_drafts=True)
    return {"commands": [c.name for c in cmds], **{k: v for k, v in result.items() if k not in ("findings", "body")}}


def _maintainer_mutation(cmd: Command):
    name, arg = cmd.name, cmd.args.split(" ")[0] if cmd.args else ""

    def apply(state: dict) -> None:
        if name == "ready":
            state["ready_override"] = True
        elif name == "unready":
            state.pop("ready_override", None)
        elif name == "pause":
            state["paused"] = True
        elif name == "resume":
            state.pop("paused", None)
        elif name == "ignore" and arg:
            state["ignored"] = sorted(set(state.get("ignored", [])) | {arg})
        elif name == "unignore" and arg:
            state["ignored"] = sorted(set(state.get("ignored", [])) - {arg})
        elif name == "resolve" and arg:
            state.setdefault("disputes", {}).pop(arg, None)
            state["ignored_keys"] = sorted(set(state.get("ignored_keys", [])) | {arg})

    return apply
