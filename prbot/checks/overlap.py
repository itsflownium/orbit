"""Detect open PRs that duplicate or overlap this one.

Uses one GraphQL query per 50 open PRs (files included), cached for the whole run.
"""

from __future__ import annotations

import re

from ..models import Finding
from . import Context, check

_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: 50, after: $cursor, orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes { number title author { login } files(first: 100) { nodes { path } } }
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
    key = ("open_prs", ctx.pr.repo)
    if key in ctx.shared:
        return ctx.shared[key]
    limit = ctx.cfg.check("overlap")["max_prs_scanned"]
    owner, name = ctx.pr.repo.split("/", 1)
    out, cursor = [], None
    while len(out) < limit:
        data = ctx.gh.graphql(_QUERY, {"owner": owner, "name": name, "cursor": cursor})
        prs = data["data"]["repository"]["pullRequests"]
        for node in prs["nodes"]:
            out.append({"number": node["number"], "title": node["title"],
                        "author": (node.get("author") or {}).get("login"),
                        "files": {f["path"] for f in (node.get("files") or {}).get("nodes", [])}})
        if not prs["pageInfo"]["hasNextPage"]:
            break
        cursor = prs["pageInfo"]["endCursor"]
    ctx.shared[key] = out[:limit]
    return ctx.shared[key]


@check("overlap")
def overlap(ctx: Context) -> list[Finding]:
    cfg = ctx.cfg.check("overlap")
    index = open_pr_index(ctx)
    mine_all = {f.filename for f in ctx.files}
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
        if other["number"] == ctx.pr.number:
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
        return []
    hits.sort(key=lambda h: -h[0])
    lines = []
    for _, other, shared, title_sim in hits[:5]:
        why = []
        if shared:
            why.append(f"{len(shared)} shared file(s): " + ", ".join(f"`{p}`" for p in sorted(shared)[:3]))
        if title_sim >= cfg["title_similarity"]:
            why.append("similar title")
        by = f" by `{other['author']}`" if other.get("author") else ""  # no @: do not ping people
        lines.append(f"- #{other['number']} {other['title']}{by} ({'; '.join(why)})")
    return [Finding(
        check="overlap", severity=ctx.severity("overlap"), key="overlap",
        title=f"May duplicate {len(hits)} other open PR(s)",
        detail="These open PRs change the same files or look like the same change. If one already solves "
               "the problem, consider coordinating or closing the duplicate:\n\n" + "\n".join(lines),
    )]
