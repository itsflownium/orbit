"""Checks that only need GitHub API data."""

from __future__ import annotations

import re
from collections import Counter

from ..diffutil import added_lines, glob_match, top_dir
from ..models import ADVISORY, BLOCKING, Finding
from . import Context, check

FAILED = {"failure", "timed_out", "startup_failure"}
NEEDS_ACTION = {"action_required"}


def ci_snapshot(gh, pr, shared: dict) -> tuple[list[dict], list[dict]]:
    """Check runs and commit statuses for the PR head, fetched once per run."""
    key = ("ci", pr.repo, pr.head_sha)
    if key not in shared:
        shared[key] = (gh.check_runs(pr.repo, pr.head_sha),
                       gh.combined_status(pr.repo, pr.head_sha).get("statuses", []))
    return shared[key]


def ci_fingerprint(gh, pr, cfg, shared: dict) -> str:
    """Changes whenever any CI result changes, e.g. a re-run on the same commit."""
    ignore = set(cfg.check("ci").get("ignore", [])) | {cfg["status_context"]}
    runs, statuses = ci_snapshot(gh, pr, shared)
    parts = sorted(f"r|{r.get('name')}|{r.get('status')}|{r.get('conclusion')}" for r in runs
                   if r.get("name") not in ignore)
    parts += sorted(f"s|{st.get('context')}|{st.get('state')}" for st in statuses if st.get("context") not in ignore)
    import hashlib
    return hashlib.sha1("\n".join(parts).encode()).hexdigest()[:12]


@check("ci")
def ci(ctx: Context) -> list[Finding]:
    cfg = ctx.cfg.check("ci")
    ignore = set(cfg.get("ignore", [])) | {ctx.cfg["status_context"]}
    pr, gh = ctx.pr, ctx.gh
    failed, pending, action = [], [], []
    runs, statuses = ci_snapshot(gh, pr, ctx.shared)

    for run in runs:
        if run.get("name") in ignore:
            continue
        if run.get("status") != "completed":
            pending.append(run["name"])
        elif run.get("conclusion") in FAILED:
            failed.append(run)
        elif run.get("conclusion") in NEEDS_ACTION:
            action.append(run["name"])

    for st in statuses:
        if st.get("context") in ignore:
            continue
        if st.get("state") in ("failure", "error"):
            failed.append({"name": st["context"], "details_url": st.get("target_url"),
                           "output": {"summary": st.get("description")}, "_status": True})
        elif st.get("state") == "pending":
            pending.append(st["context"])

    gates = [re.compile(p) for p in cfg.get("aggregate_patterns", [])]
    real = [r for r in failed if not any(g.search(r["name"]) for g in gates)]
    if real:
        failed = real  # a gate job only repeats the failures already listed

    findings: list[Finding] = []
    for i, run in enumerate(failed):
        lines = [f"[Details]({run['details_url']})" if run.get("details_url") else ""]
        summary = str((run.get("output") or {}).get("summary") or "").strip()
        if summary:
            lines.append(summary[:600])
        # GitHub Actions check-run ids are job ids, so we can show the end of the log.
        if cfg.get("show_logs", True) and i < 2 and not run.get("_status") \
                and (run.get("app") or {}).get("slug") == "github-actions":
            tail = gh.job_log_tail(pr.repo, run["id"])
            if tail:
                lines.append("<details><summary>End of log</summary>\n\n```\n" + _clean_log(tail) + "\n```\n</details>")
        findings.append(Finding(
            check="ci", severity=ctx.severity("ci"), key=f"ci-{_slug(run['name'])}",
            title=f"CI job `{run['name']}` is failing",
            detail="\n\n".join(x for x in lines if x),
        ))
    for name in action:
        findings.append(Finding(check="ci", severity=ADVISORY, key=f"ci-action-{_slug(name)}",
                                title=f"`{name}` is waiting for maintainer approval to run"))
    if (pending or action) and ctx.severity("ci") == BLOCKING:
        ctx.pending_checks.add("ci")
    if pending:
        ctx.notes.append(f"CI still running ({', '.join(sorted(set(pending))[:5])}); the bot re-checks when it finishes.")
    return findings


@check("mergeable")
def mergeable(ctx: Context) -> list[Finding]:
    pr = ctx.pr
    if pr.mergeable is None or pr.mergeable_state == "unknown":
        ctx.notes.append("GitHub is still computing whether this PR merges cleanly; the bot re-checks on its next run.")
        return []
    if pr.mergeable is False or pr.mergeable_state == "dirty":
        return [Finding(
            check="mergeable", severity=ctx.severity("mergeable"), key="mergeable-conflict",
            title=f"Merge conflicts with `{pr.base_ref}`",
            detail=(f"Rebase or merge the latest `{pr.base_ref}` and resolve the conflicts:\n\n"
                    f"```bash\ngit fetch origin {pr.base_ref}\ngit rebase origin/{pr.base_ref}\n"
                    f"# resolve conflicts, then\ngit push --force-with-lease\n```"),
        )]
    if pr.mergeable_state == "behind":
        return [Finding(
            check="mergeable", severity=ctx.cfg.check("mergeable").get("behind_severity", ADVISORY),
            key="mergeable-behind", fixable=True,
            title=f"Branch is behind `{pr.base_ref}`",
            detail="There are no conflicts; the branch just needs the latest base changes.",
        )]
    return []


@check("size")
def size(ctx: Context) -> list[Finding]:
    cfg = ctx.cfg.check("size")
    files = ctx.files
    lines = sum(f.additions + f.deletions for f in files)
    if lines <= cfg["max_lines"] and len(files) <= cfg["max_files"]:
        return []
    by_dir = Counter()
    for f in files:
        by_dir[top_dir(f.filename)] += f.additions + f.deletions
    breakdown = "\n".join(f"- `{d}`: {n} lines" for d, n in by_dir.most_common(8))
    return [Finding(
        check="size", severity=ctx.severity("size"), key="size-large",
        title=f"Large PR: {lines} changed lines across {len(files)} files",
        detail=("Large PRs take much longer to review. If these changes are independent, "
                f"consider splitting them:\n\n{breakdown}"),
    )]


@check("tests")
def tests(ctx: Context) -> list[Finding]:
    cfg = ctx.cfg.check("tests")
    test_patterns = cfg["test_patterns"]
    exts = tuple(cfg["code_extensions"])
    required = cfg.get("required_paths") or []
    files = [f for f in ctx.files if f.status != "removed"]
    test_files = [f for f in files if glob_match(f.filename, test_patterns)]
    code_files = [
        f for f in files
        if f.filename.endswith(exts) and f not in test_files
        and (not required or glob_match(f.filename, required))
    ]
    code_lines = sum(f.additions for f in code_files)
    if test_files or not code_files or code_lines < cfg["min_code_lines"]:
        return []
    listed = "\n".join(f"- `{f.filename}` (+{f.additions})" for f in code_files[:10])
    severity = ctx.severity("tests")
    if any(re.search(p, ctx.pr.title, re.I) for p in cfg.get("blocking_title_patterns") or []):
        severity = "blocking"
    return [Finding(
        check="tests", severity=severity, key="tests-missing",
        title="Code changed but no tests were added or updated",
        detail=f"These files changed without matching test changes:\n\n{listed}\n\n"
               "Add a test that covers the new behaviour, or reply `/bot dispute tests-missing <reason>` "
               "if tests are not needed here.",
    )]


_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_ISSUE_REF = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|refs?|related to)\b[:\s]+"
                        r"(?:[\w.-]+/[\w.-]+)?#\d+|github\.com/[\w.-]+/[\w.-]+/issues/\d+")


_CHECKBOX = re.compile(r"^\s*[-*]\s*\[[ xX]\]\s")  # ticking a template checkbox is not a description
_BARE = re.compile(r"^\s*(?:[-*]|\d+\.|#+|(?:fixes|closes|resolves)\s*#?\d*)\s*$", re.I)


def _norm(line: str) -> str:
    return re.sub(r"\[[xX]\]", "[ ]", line.strip())


@check("description")
def description(ctx: Context) -> list[Finding]:
    cfg = ctx.cfg.check("description")
    pr = ctx.pr
    body = _HTML_COMMENT.sub("", pr.body)
    template = ctx.shared.get(("template", pr.repo))
    if template is None:
        template = ""
        for path in (".github/pull_request_template.md", ".github/PULL_REQUEST_TEMPLATE.md",
                     "pull_request_template.md", "docs/pull_request_template.md"):
            text = ctx.gh.get_file_text(pr.repo, path, pr.base_ref)
            if text:
                template = text
                break
        ctx.shared[("template", pr.repo)] = template
    template_lines = {_norm(ln) for ln in _HTML_COMMENT.sub("", template).splitlines() if ln.strip()}
    own = [ln for ln in body.splitlines()
           if ln.strip() and _norm(ln) not in template_lines and not _CHECKBOX.match(ln) and not _BARE.match(ln)]
    written = re.sub(r"\s+", " ", " ".join(own)).strip()
    out: list[Finding] = []
    if len(written) < cfg["min_length"]:
        out.append(Finding(
            check="description", severity=ctx.severity("description"), key="description-empty",
            title="PR description is empty or only the template",
            detail="Explain what this changes and why, and how you tested it. "
                   "Reviewers prioritise PRs they can understand quickly.",
        ))
    if cfg.get("require_linked_issue") and not _ISSUE_REF.search(body):
        out.append(Finding(
            check="description", severity=ctx.severity("description"), key="description-no-issue",
            title="No linked issue",
            detail="Link the issue this PR addresses, e.g. `Fixes #123`.",
        ))
    return out


_SECRET_PATTERNS = [
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b|\bgithub_pat_[A-Za-z0-9_]{60,}\b")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("OpenAI-style API key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}\b")),
    ("Hugging Face token", re.compile(r"\bhf_[A-Za-z0-9]{30,}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
]
_CONFLICT = re.compile(r"^(?:<{7}|>{7})(?: |$)")
_SENSITIVE_FILES = re.compile(r"(?:^|/)(?:\.env(?:\.[\w-]+)?|id_rsa|id_ed25519|.*\.pem|.*\.p12|.*\.pfx|credentials\.json)$")
_SAFE_ENV = re.compile(r"\.env\.(?:example|sample|template|dist)$")
_JUNK_FILES = re.compile(r"(?:^|/)(?:\.DS_Store|Thumbs\.db|.*\.pyc|.*\.swp|.*~)$")
_BINARY_EXT = re.compile(r"\.(?:zip|tar|gz|tgz|7z|rar|exe|dll|so|dylib|bin|jar|whl|pkl|pt|pth|ckpt|safetensors|onnx|npy|npz|h5)$", re.I)


@check("debris")
def debris(ctx: Context) -> list[Finding]:
    sev = ctx.severity("debris")
    out: list[Finding] = []
    for f in ctx.files:
        if f.status == "removed":
            continue
        name = f.filename
        if _SENSITIVE_FILES.search(name) and not _SAFE_ENV.search(name):
            out.append(Finding(check="debris", severity=sev, path=name, key=f"debris-sensitive-{_slug(name)}",
                               title=f"Sensitive file committed: `{name}`",
                               detail="Remove it from the PR and rotate any credentials it contains."))
        elif _JUNK_FILES.search(name):
            out.append(Finding(check="debris", severity=ADVISORY, path=name, key=f"debris-junk-{_slug(name)}",
                               title=f"Unintended file: `{name}`",
                               detail="This looks like an editor or OS artifact; remove it and add it to `.gitignore`."))
        elif f.patch is None and f.status == "added" and _BINARY_EXT.search(name):
            out.append(Finding(check="debris", severity=ADVISORY, path=name, key=f"debris-binary-{_slug(name)}",
                               title=f"Binary file added: `{name}`",
                               detail="Binary artifacts usually belong in releases or external storage, not the repo."))
        for line_no, text in added_lines(f.patch).items():
            if _CONFLICT.match(text):
                out.append(Finding(check="debris", severity=sev, path=name, line=line_no,
                                   key=f"debris-conflict-{_slug(name)}",
                                   title=f"Leftover merge-conflict marker in `{name}`",
                                   detail="Resolve the conflict and remove the `<<<<<<<` / `>>>>>>>` markers."))
                break
        hits: list[tuple[int, str]] = []
        for line_no, text in added_lines(f.patch).items():
            for label, pattern in _SECRET_PATTERNS:
                if pattern.search(text):
                    hits.append((line_no, label))
                    break
        if hits:  # one finding per file, however many lines match
            first, label = hits[0]
            kinds = sorted({lbl for _, lbl in hits})
            lines = ", ".join(str(n) for n, _ in hits[:10]) + (f" and {len(hits) - 10} more" if len(hits) > 10 else "")
            out.append(Finding(check="debris", severity=sev, path=name, line=first,
                               key=f"debris-secret-{_slug(name)}",
                               title=f"Possible {' / '.join(kinds)} in `{name}`",
                               detail=f"Line(s) {lines}. Never commit credentials. Remove them, **rotate the keys**, "
                                      "and load them from environment variables instead. If these are test "
                                      "fixtures, reply `/bot dispute debris-secret-... <reason>`."))
    return out


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48] or "x"


_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_TS = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ")


def _clean_log(text: str) -> str:
    lines = [_TS.sub("", _ANSI.sub("", ln)) for ln in text.splitlines()]
    return "\n".join(lines).replace("```", "'''")[-4000:]
