"""Detect changes already on the default branch and open PRs with the same or similar changes.

Uses one GraphQL query per 50 open PRs (files included), cached for the whole run.
"""

from __future__ import annotations

import hashlib
import json
import re

from ..diffutil import glob_match
from ..github import GitHubError, cached_tree
from ..models import Finding
from ..workspace import server_url
from . import Context, check

_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: 50, after: $cursor, orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title headRefOid baseRefName changedFiles author { login }
        files(first: 100) { nodes { path } pageInfo { hasNextPage endCursor } }
      }
    }
  }
}
"""

_FILE_PAGE = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      files(first: 100, after: $cursor) { nodes { path } pageInfo { hasNextPage endCursor } }
    }
  }
}
"""

_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "and", "or", "to", "of", "in", "for", "on", "with", "fix", "add", "update",
         "feat", "chore", "docs", "refactor", "is", "from", "by", "pr"}


def _tokens(title: str) -> set[str]:
    return {w for w in _WORD.findall(title.lower()) if w not in _STOP and len(w) > 1}


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def open_pr_index(ctx: Context) -> list[dict]:
    cfg = ctx.cfg.check("overlap")
    limit = cfg["max_prs_scanned"]
    file_limit = cfg.get("max_files_per_pr", 100)
    key = ("open_prs", ctx.pr.repo, limit, file_limit)
    if key in ctx.shared:
        return ctx.shared[key]
    owner, name = ctx.pr.repo.split("/", 1)
    out, seen, cursor = [], set(), None
    while len(out) < limit:
        data = ctx.gh.graphql(_QUERY, {"owner": owner, "name": name, "cursor": cursor})
        prs = data["data"]["repository"]["pullRequests"]
        for node in prs["nodes"]:
            if len(out) >= limit:
                break
            if node["number"] in seen:
                continue
            seen.add(node["number"])
            files = node.get("files") or {}
            paths = [f["path"] for f in files.get("nodes", [])][:file_limit]
            page = files.get("pageInfo") or {}
            while len(paths) < file_limit and page.get("hasNextPage"):
                data = ctx.gh.graphql(_FILE_PAGE, {"owner": owner, "name": name, "number": node["number"],
                                                   "cursor": page["endCursor"]})
                files = data["data"]["repository"]["pullRequest"]["files"]
                paths.extend(f["path"] for f in files["nodes"][:file_limit - len(paths)])
                page = files["pageInfo"]
            out.append({"number": node["number"], "title": node["title"],
                        "author": (node.get("author") or {}).get("login"),
                        "head": node.get("headRefOid"), "base": node.get("baseRefName"),
                        "changed_files": node.get("changedFiles"),
                        "files": set(paths)})
        if not prs["pageInfo"]["hasNextPage"]:
            break
        cursor = prs["pageInfo"]["endCursor"]
    ctx.shared[key] = out
    return out


def overlap_fingerprint(ctx: Context) -> str:
    cfg = ctx.cfg.check("overlap")
    key = ("overlap_fp", ctx.pr.repo, cfg["max_prs_scanned"], cfg["max_files_per_pr"])
    if key not in ctx.shared:
        index = open_pr_index(ctx)
        comparable = sorted((r["number"], r["title"], r["head"], r["base"], sorted(r["files"])) for r in index)
        ctx.shared[key] = hashlib.sha1(json.dumps(comparable).encode()).hexdigest()[:12]
    return ctx.shared[key]


def _same_files(ctx: Context, ref: str) -> bool:
    pr = ctx.pr
    files = pr.files
    if not files or len(files) != pr.changed_files or len(files) > ctx.cfg.check("overlap")["max_files_per_pr"]:
        return False
    target, target_complete = cached_tree(ctx.gh, pr.repo, ref, ctx.shared)
    if not target_complete:
        return False
    head, head_complete = cached_tree(ctx.gh, pr.repo, pr.head_sha, ctx.shared)
    if not head_complete:
        return False
    for file in files:
        path = file.filename
        if file.status == "removed":
            if path in target or path in head:
                return False
        elif path not in head or target.get(path) != head[path]:
            return False
        if file.status == "renamed" and (not file.previous_filename or file.previous_filename in target
                                        or file.previous_filename in head):
            return False
    return True


def _on_default_branch(ctx: Context) -> Finding | None:
    pr = ctx.pr
    default = pr.raw["base"]["repo"].get("default_branch")
    if not default or pr.base_ref != default or not pr.base_sha or not pr.head_sha:
        return None
    matching = _same_files(ctx, pr.base_sha)
    if not pr.changed_files and not pr.files:
        try:
            matching = ctx.gh.compare_status(pr.repo, pr.base_sha, pr.head_sha) in ("behind", "identical")
        except (GitHubError, OSError):
            pass
    if matching:
        return Finding(
            check="overlap", severity=ctx.severity("overlap"), key="changes-on-default-branch",
            title=f"These changes are already present on {default}",
            detail=f"Compare with the [reviewed {default} snapshot]"
                   f"({server_url()}/{pr.repo}/tree/{pr.base_sha}) before closing the duplicate.",
        )
    return None


@check("overlap")
def overlap(ctx: Context) -> list[Finding]:
    cfg = ctx.cfg.check("overlap")
    ignore = ctx.cfg.get("ignore_paths", [])
    index = [{**pr, "files": {path for path in pr["files"] if not glob_match(path, ignore)}}
             for pr in open_pr_index(ctx)]
    mine_all = {f.filename for f in ctx.files}
    already_present = _on_default_branch(ctx)
    findings = [already_present] if already_present else []
    all_paths = {file.filename for file in ctx.pr.files}
    same, checked = set(), 0
    for pr in index:
        if pr["number"] == ctx.pr.number or pr["base"] != ctx.pr.base_ref or not pr["head"]:
            continue
        if pr["head"] == ctx.pr.head_sha:
            same.add(pr["number"])
        elif checked < 3 and pr["files"] == all_paths and pr["changed_files"] == ctx.pr.changed_files and all_paths:
            checked += 1
            if _same_files(ctx, pr["head"]):
                same.add(pr["number"])
    if same:
        links = [f"[#{number}]({server_url()}/{ctx.pr.repo}/pull/{number})" for number in sorted(same)[:5]]
        findings.append(Finding(
            check="overlap", severity=ctx.severity("overlap"), key="same-changes-open",
            title=f"Same changes as {len(same)} open PR(s)",
            detail="Open PRs targeting this branch: " + ", ".join(links),
        ))
    # How many open PRs touch each file; hub files say nothing about duplication.
    freq: dict[str, int] = {}
    for other in index:
        for path in other["files"]:
            freq[path] = freq.get(path, 0) + 1
    hub = cfg.get("hub_file_prs", 4)
    mine = {p for p in mine_all if freq.get(p, 1) <= hub}
    my_title = _tokens(ctx.pr.title)
    hits = []
    for other in index:
        if other["number"] == ctx.pr.number or other["number"] in same:
            continue
        theirs = {p for p in other["files"] if freq.get(p, 1) <= hub}
        shared = mine & theirs
        union = mine | theirs
        file_sim = len(shared) / len(union) if union else 0.0
        title_sim = _jaccard(my_title, _tokens(other["title"]))
        any_shared = bool(mine_all & other["files"])
        if (shared and file_sim >= cfg["min_file_overlap"]) \
                or (any_shared and title_sim >= cfg["title_similarity"]) \
                or title_sim >= cfg.get("title_only_similarity", 0.85):
            hits.append((file_sim + title_sim, other, shared or (mine_all & other["files"]), title_sim))
    if not hits:
        return findings
    hits.sort(key=lambda h: -h[0])
    lines = []
    for _, other, shared, title_sim in hits[:5]:
        why = []
        if shared:
            why.append(f"{len(shared)} shared file(s): " + ", ".join(f"`{p}`" for p in sorted(shared)[:3]))
        if title_sim >= cfg["title_similarity"]:
            why.append("similar title")
        by = f" by `{other['author']}`" if other.get("author") else ""  # no @: do not ping people
        link = f"[#{other['number']}]({server_url()}/{ctx.pr.repo}/pull/{other['number']})"
        lines.append(f"- {link} {other['title']}{by} ({', '.join(why)})")
    findings.append(Finding(
        check="overlap", severity=ctx.severity("overlap"), key="overlap",
        title=f"May duplicate {len(hits)} other open PR(s)",
        detail="These open PRs change the same files or look like the same change. If one already solves "
               "the problem, consider coordinating or closing the duplicate:\n\n" + "\n".join(lines),
    ))
    return findings
