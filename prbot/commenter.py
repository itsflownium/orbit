"""One summary comment per PR (edited in place), inline review comments, labels, status."""

from __future__ import annotations

import base64
import json
import logging
import re

from .config import autofix_mode
from .github import GitHubError
from .models import Finding, PullRequest

log = logging.getLogger(__name__)

MARKER = "<!-- prbot:summary -->"
_STATE = re.compile(r"<!-- prbot:state ([A-Za-z0-9+/=]+) -->")

LABEL_STYLE = {
    "needs_changes": ("d93f0b", "Pre-review found issues the author should fix"),
    "ready": ("0e8a16", "Pre-review passed; ready for a maintainer"),
    "disputed": ("fbca04", "Author disputes a pre-review finding"),
    "duplicate": ("c5def5", "May duplicate or overlap another open PR"),
    "stale": ("ededed", "Needs changes and the author has been inactive"),
    "paused": ("bfd4f2", "Pre-review paused by a maintainer"),
}


def encode_state(state: dict) -> str:
    return base64.b64encode(json.dumps(state, separators=(",", ":")).encode()).decode()


def decode_state(body: str) -> dict:
    """The bot always writes its state block last, so only the last one counts."""
    matches = list(_STATE.finditer(body or ""))
    if not matches:
        return {}
    try:
        value = json.loads(base64.b64decode(matches[-1].group(1)))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def replace_state(body: str, state: dict) -> str:
    """Swap the trailing state block (the bot's own is always last)."""
    matches = list(_STATE.finditer(body))
    block = f"<!-- prbot:state {encode_state(state)} -->"
    if not matches:
        return body + "\n" + block
    m = matches[-1]
    return body[:m.start()] + block + body[m.end():]


def is_bot_comment(gh, comment: dict) -> bool:
    login = ((comment.get("user") or {}).get("login") or "").lower()
    return bool(login) and login == (gh.login() or "").lower()


def summary_in(gh, comments: list[dict]) -> tuple[int | None, dict]:
    """The bot's own summary comment. Comments by anyone else are ignored, so the state
    (ready override, ignored checks, disputes) cannot be forged by posting the marker."""
    for c in comments:
        if MARKER in (c.get("body") or "") and is_bot_comment(gh, c):
            return c["id"], decode_state(c["body"])
    return None, {}


find_summary_from = summary_in


def find_summary(gh, pr: PullRequest) -> tuple[int | None, dict]:
    return summary_in(gh, gh.issue_comments(pr.repo, pr.number))


_MENTION = re.compile(r"@(?=[A-Za-z0-9])")
_SUGGESTION_FENCE = re.compile(r"([`~]{3,}[ \t]*)suggestion\b", re.I)


def clean(text: str | None) -> str:
    """Neutralise untrusted text (model output, CI logs, dispute reasons, titles) before posting:
    no HTML comments that could fake bot state, no @-mentions that would ping people."""
    if not text:
        return ""
    text = text.replace("<!--", "&lt;!--").replace("-->", "--&gt;")
    text = _SUGGESTION_FENCE.sub(r"\1text", text)
    return _MENTION.sub("@\u200b", text)


def _fence(text: str) -> str:
    longest = max((len(m) for m in re.findall(r"`{3,}", text)), default=0)
    return "`" * max(3, longest + 1)


def _details(f: Finding) -> str:
    parts = [clean(f.detail)]
    if f.references:
        parts.append("**Related code**\n\n" + "\n".join(f"- {link}" for link in f.references))
    if f.recommendation:
        parts.append("**How to fix**\n\n" + clean(f.recommendation))
    return "\n\n".join(part for part in parts if part)


def inline_body(f: Finding, *, allow_suggestions: bool = True) -> str:
    parts = [f"**{clean(f.title)}**"]
    details = _details(f)
    if details:
        parts.append(details)
    if f.suggestion is not None and allow_suggestions:
        fence = _fence(f.suggestion)
        # an empty suggestion block deletes the selected lines; "\n\n" would leave a blank line
        inner = f"{f.suggestion}\n" if f.suggestion else ""
        parts.append(f"{fence}suggestion\n{inner}{fence}")
    elif f.suggestion:
        example = clean(f.suggestion)
        fence = _fence(example)
        parts.append(f"**Example replacement**\n\n{fence}\n{example}\n{fence}")
    if f.check != "autofix":
        parts.append("<sub>prbot · reply `/bot dispute " + _key(f.key) + " <reason>` if this is wrong</sub>")
    return "\n\n".join(parts)


def _item(f: Finding, checked: bool = False, compact: bool = False) -> str:
    box = "[x]" if checked else "[ ]"
    where = f" — `{f.path}`" + (f" line {f.line}" if f.line else "") if f.path and f.check != "lint" else ""
    head = f"- {box} {clean(f.title)}{clean(where)} <sub>`{_key(f.key)}`</sub>"
    details = _details(f)
    if details and not checked and not compact:
        detail = "\n".join("  " + ln if ln else "" for ln in details.split("\n"))
        head += f"\n  <details><summary>Details</summary>\n\n{detail}\n\n  </details>"
    return head


MAX_BODY = 60_000  # GitHub rejects comments over 65,536 characters
MAX_ITEMS = 40


def _key(key: str | None) -> str:
    """Finding keys are rendered in code spans; keep them to a safe alphabet."""
    return re.sub(r"[^A-Za-z0-9._/-]", "-", key or "")[:80]


def render(pr: PullRequest, findings: list[Finding], notes: list[str], state: dict, cfg: dict,
           fixed: list[str] | None = None, triage: dict | None = None) -> str:
    for compact in (False, True):
        body = _render(pr, findings, notes, state, cfg, fixed, triage, compact)
        if len(body) <= MAX_BODY:
            return body
    # still too long: keep the head and the state block (the state must always survive)
    tail = f"<!-- prbot:state {encode_state(state)} -->"
    return body[: MAX_BODY - len(tail) - 200] + "\n\n_(truncated: too many findings to list)_\n" + tail


def _render(pr, findings, notes, state, cfg, fixed, triage, compact) -> str:
    disputes = state.get("disputes", {})
    blocking = [f for f in findings if f.blocking]
    advisory = [f for f in findings if not f.blocking and f.key not in disputes and f.check != "autofix"]
    disputed = [f for f in findings if f.key in disputes]
    posted_inline = set(state.get("posted_inline", []))
    suggestions = [f for f in findings if f.check == "autofix" and f.key in posted_inline]
    override = state.get("ready_override")

    def items(fs):
        shown = [_item(f, compact=compact) for f in fs[:MAX_ITEMS]]
        if len(fs) > MAX_ITEMS:
            shown.append(f"- ...and {len(fs) - MAX_ITEMS} more")
        return shown

    out = [MARKER]
    if state.get("paused"):
        out.append("### Pre-review paused\n\nA maintainer paused automated review on this PR. "
                   "A maintainer can comment `/bot resume` to turn it back on.")
    elif state.get("fix_pending"):
        out.append("### Pre-review: fix submitted\n\nThis PR will be checked again after the branch updates.")
    elif blocking and not override:
        n = len(blocking)
        out.append(f"### Pre-review: {n} item{'s' if n != 1 else ''} to fix before maintainer review\n")
        out.append("Address the blocking findings below and push an update to rerun pre-review.")
        out.append("\n**Must fix**\n")
        out.extend(items(blocking))
    elif override:
        out.append("### Pre-review passed\n")
        out.append("A maintainer marked this PR ready.")
    elif state.get("dispute_pending"):
        out.append("### Pre-review: waiting for a maintainer\n")
        out.append("The remaining findings are disputed. A maintainer decides with `/bot resolve <key>` "
                   "(accept the dispute) or `/bot ready`.")
    elif state.get("lint_pending"):
        out.append("### Pre-review: waiting for lint results\n")
        out.append("No blocking issues so far. The lint analysis for this commit has not reported yet; "
                   "this comment updates when it does.")
    elif state.get("pending_checks"):
        out.append("### Pre-review: waiting for required checks\n\n"
                   "See the checks and run details for the latest status.")
    elif state.get("llm_pending") and not state.get("ready"):
        out.append("### Pre-review: waiting for model review\n\n"
                   "The bot will retry on the next review run.")
    else:
        out.append("### Pre-review passed\n")
        out.append("No blocking issues found. This PR is in the maintainers' review queue.")
    if fixed:
        out.append("\n**Fixed since last check**\n")
        out.extend(f"- [x] {clean(title)}" for title in fixed[:MAX_ITEMS])
    if suggestions:
        out.append(f"\n**Suggested fixes:** {len(suggestions)} inline suggestion(s) posted; "
                   "apply them with *Commit suggestion* in the Files tab.")
    if advisory:
        out.append("\n**Advisory findings**\n")
        out.extend(items(advisory))
    if disputed:
        out.append("\n**Disputed** (a maintainer will decide)\n")
        for f in disputed[:MAX_ITEMS]:
            out.append(f"- {clean(f.title)} <sub>`{_key(f.key)}`</sub> — _{clean(disputes[f.key])}_")
    if triage:
        out.append(f"\n<sub>Triage: {clean(str(triage.get('change_type', '?')))}, "
                   f"risk {clean(str(triage.get('risk', '?')))}</sub>")
    if notes and not compact:
        out.append("\n<details><summary>Run details</summary>\n\n"
                   + "\n".join(f"- {clean(n)}" for n in notes[:30]) + "\n\n</details>")
    fix_command = ("- `/bot fix` — ask the bot to apply available lint fixes\n"
                   if autofix_mode(cfg) != "comment" else "")
    out.append(
        "\n<details><summary>Commands</summary>\n\n"
        "- `/bot recheck` — run the checks again\n"
        f"{fix_command}"
        "- `/bot dispute <key> <reason>` — flag a finding as wrong; a maintainer decides\n"
        "- Maintainers: `/bot ready`, `/bot resolve <key>`, `/bot pause`, `/bot resume`, `/bot ignore <check>`\n\n"
        "</details>"
    )
    out.append(f"\n<sub>Automated pre-review of {pr.head_sha[:7]} by prbot. Maintainers make the final call.</sub>")
    out.append(f"<!-- prbot:state {encode_state(state)} -->")
    return "\n".join(out)


def ensure_labels(gh, repo: str, cfg: dict, shared: dict) -> None:
    key = ("labels_ok", repo)
    if shared.get(key):
        return
    for name, (color, desc) in LABEL_STYLE.items():
        try:
            gh.ensure_label(repo, cfg["labels"][name], color, desc)
        except GitHubError as e:
            log.warning("could not create label %s: %s", cfg["labels"][name], e)
    shared[key] = True


def sync_labels(gh, pr: PullRequest, cfg: dict, wanted: set[str], shared: dict,
                current: set[str] | None = None) -> None:
    ensure_labels(gh, pr.repo, cfg, shared)
    managed = {cfg["labels"][k] for k in LABEL_STYLE}
    want = {cfg["labels"][k] for k in wanted}
    current = set(pr.labels) if current is None else current
    add = sorted(want - current)
    if add:
        gh.add_labels(pr.repo, pr.number, add)
    for lbl in sorted((managed & current) - want):
        gh.remove_label(pr.repo, pr.number, lbl)


def post_inline(gh, pr: PullRequest, findings: list[Finding], state: dict, limit: int,
                *, allow_suggestions: bool = True) -> int:
    posted = set(state.get("posted_inline", []))
    todo = [f for f in findings if f.path and f.line and f.key not in posted][:limit]
    if not todo:
        return 0

    def payload(f: Finding) -> dict:
        c = {"path": f.path, "line": f.line, "side": "RIGHT",
             "body": inline_body(f, allow_suggestions=allow_suggestions)}
        if f.start_line and f.start_line < f.line:
            c["start_line"], c["start_side"] = f.start_line, "RIGHT"
        return c

    try:
        gh.create_review(pr.repo, pr.number, pr.head_sha, [payload(f) for f in todo])
        done = todo
    except GitHubError as e:
        log.info("batched review failed (%s); posting comments one by one", e)
        done = []
        for f in todo:
            try:
                gh.create_review(pr.repo, pr.number, pr.head_sha, [payload(f)])
                done.append(f)
            except GitHubError as e2:
                log.info("inline comment on %s:%s rejected: %s", f.path, f.line, e2)
    order = [k for k in state.get("posted_inline", []) if isinstance(k, str)]
    order += [f.key for f in done if f.key not in posted]
    state["posted_inline"] = order[-200:]  # trim the oldest, not alphabetically
    return len(done)


def upsert_summary(gh, pr: PullRequest, comment_id: int | None, body: str) -> None:
    if comment_id:
        try:
            gh.update_comment(pr.repo, comment_id, body)
            return
        except GitHubError as e:
            if e.status != 404:  # deleted meanwhile: post a new one
                raise
    gh.create_comment(pr.repo, pr.number, body)
