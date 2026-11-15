"""Orchestrates one PR review: commands -> checks -> fixes -> model review -> comment/labels/status.

This runs in the privileged context and never executes PR code. Lint results come from the
analysis job (prbot/analyzer.py), either passed in or downloaded as an artifact.

Outcomes, and the commit status each one sets:
  blocking findings                         -> failure  + bot:needs-changes
  only disputed blockers left               -> pending  + bot:disputed (a maintainer decides)
  clean, but lint analysis not reported yet -> pending  (no ready label)
  clean, or a maintainer said /bot ready    -> success  + bot:ready-for-review
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

from . import analyzer, commands, commenter, llm
from .checks import Context, run_checks
from .checks.basic import ci_fingerprint
from .config import Config, load_config, parse_config
from .fixer import apply_fixes
from .models import ADVISORY, BLOCKING, Finding

log = logging.getLogger(__name__)

# state keys that /bot commands set; a concurrent change to any of them restarts the review
COMMAND_KEYS = ("disputes", "ignored", "ignored_keys", "paused", "ready_override", "cmd_seen")
FINGERPRINT_KEYS = ("head_sha", "base_ref", "mergeable_state", "ci_fp", "config_hash")


class _Restart(Exception):
    pass


def _now() -> float:
    return time.time()


def _config_hash(cfg: Config) -> str:
    return hashlib.sha1(json.dumps(cfg, sort_keys=True, default=str).encode()).hexdigest()[:10]


def _guidelines(gh, pr, shared: dict) -> str | None:
    key = ("guidelines", pr.repo, pr.base_ref)
    if key not in shared:
        text = None
        for path in ("CONTRIBUTING.md", ".github/CONTRIBUTING.md", "docs/CONTRIBUTING.md", "AGENTS.md", "AGENTS.md"):
            text = gh.get_file_text(pr.repo, path, pr.base_ref)
            if text:
                break
        shared[key] = text
    return shared[key]


def _load_cfg(gh, pr, shared: dict, notes: list[str]) -> Config:
    key = ("config", pr.repo, pr.base_ref)
    err_key = ("config_error", pr.repo, pr.base_ref)
    if key not in shared:
        try:
            shared[key] = load_config(gh, pr.repo, pr.base_ref)
        except ValueError as e:  # invalid YAML or shape: keep reviewing with defaults
            log.warning("invalid prereview config: %s", e)
            shared[key] = parse_config(None)
            shared[err_key] = str(e)
    if shared.get(err_key):
        notes.append(f"The repo's prereview config is invalid and was ignored: {shared[err_key]}")
    return shared[key]


def _analysis_required(cfg: Config) -> bool:
    a = cfg.get("analysis", {})
    return bool(a.get("required", True) and a.get("artifact", True)
                and cfg.check_enabled("lint") and cfg.get("execution") != "never")


def _analysis(gh, pr, cfg: Config, state: dict, given: Any) -> analyzer.Analysis | None:
    """Lint results for this head: passed in, cached in state, or downloaded."""
    if given is not None:
        a = given if isinstance(given, analyzer.Analysis) else analyzer.load_analysis(given, pr)
        if a is not None:
            return a
    cached = state.get("lint") or {}
    if cached.get("sha") == pr.head_sha:
        data = {"version": analyzer.VERSION, "repo": pr.repo, "number": pr.number, "head_sha": pr.head_sha,
                "findings": cached.get("findings", []), "notes": cached.get("notes", []), "patch": ""}
        a = analyzer.load_analysis(data, pr)
        if a is not None and not a.findings:
            return a
        # findings exist: the patch is needed for fixes, so try the artifact, else use the cache
        return _download(gh, pr, cfg) or a
    return _download(gh, pr, cfg)


def _download(gh, pr, cfg: Config) -> analyzer.Analysis | None:
    a = cfg.get("analysis", {})
    if not a.get("artifact", True):
        return None
    try:
        return analyzer.fetch_analysis(gh, pr, a.get("workflow"))
    except Exception as e:  # API errors, bad zip
        log.info("could not download analysis artifact: %s", e)
        return None


def _fingerprint(state: dict, pr, ci_fp: str, cfg_hash: str) -> None:
    state.update(head_sha=pr.head_sha, base_ref=pr.base_ref, mergeable_state=pr.mergeable_state,
                 ci_fp=ci_fp, config_hash=cfg_hash,
                 merge_pending=pr.mergeable is None or pr.mergeable_state == "unknown")


def review_pr(gh, repo: str, number: int, *, workspace: str | None = None, shared: dict | None = None,
              force: bool = False, include_drafts: bool = False, force_fix: bool = False,
              analysis: Any = None, analyze_fn: Callable[[Any, Config], Any] | None = None,
              mutate_state: Callable[[dict], None] | None = None) -> dict[str, Any]:
    """Review one PR. If another run changes the PR's command state meanwhile, start over so the
    comment, status and labels always come from one consistent state."""
    shared = shared if shared is not None else {}
    for attempt in range(3):
        try:
            return _review_once(gh, repo, number, workspace=workspace, shared=shared, force=force,
                                include_drafts=include_drafts, force_fix=force_fix, analysis=analysis,
                                analyze_fn=analyze_fn, mutate_state=mutate_state, last_try=attempt == 2)
        except _Restart:
            log.info("PR #%s changed during the review; starting over", number)
    raise RuntimeError("unreachable")


def _review_once(gh, repo, number, *, workspace, shared, force, include_drafts, force_fix, analysis,
                 analyze_fn, mutate_state, last_try) -> dict[str, Any]:
    pr = gh.get_pr(repo, number)
    result: dict[str, Any] = {"repo": repo, "number": number, "title": pr.title, "author": pr.author}
    if pr.raw.get("state", "open") != "open":
        return {**result, "skipped": "closed"}

    notes: list[str] = []
    cfg = _load_cfg(gh, pr, shared, notes)
    if not cfg.get("enabled", True):
        return {**result, "skipped": "disabled by config"}
    if pr.author in cfg.get("skip_authors", []):
        return {**result, "skipped": "author skipped"}

    comments = gh.issue_comments(pr.repo, pr.number)
    comment_id, state = commenter.summary_in(gh, comments)
    loaded = copy.deepcopy(state)  # snapshot, to detect a concurrent run's changes

    # ---- /bot commands posted since the last run (even if their own run was cancelled)
    pending = commands.collect(gh, pr.repo, pr.author, comments, state)
    for m in pending.mutations:
        m(state)
    if pending.comment_ids:
        state["cmd_seen"] = pending.last_id
        force = True
        include_drafts = True
    if mutate_state:
        mutate_state(state)
    force_fix = force_fix or pending.force_fix

    if pr.draft and cfg.get("skip_drafts", True) and not include_drafts:
        return {**result, "skipped": "draft"}

    cfg_hash = _config_hash(cfg)
    ci_fp = ci_fingerprint(gh, pr, cfg, shared) if cfg.check_enabled("ci") else ""
    if state.get("head_sha") != pr.head_sha:
        state["head_since"] = _now()
    stale_days = cfg["comments"].get("stale_days") or 0
    is_stale = bool(state.get("titles")) and bool(stale_days) and \
        _now() - state.get("head_since", _now()) > stale_days * 86400

    unchanged = (all(state.get(k) == v for k, v in (("head_sha", pr.head_sha), ("base_ref", pr.base_ref),
                                                    ("mergeable_state", pr.mergeable_state), ("ci_fp", ci_fp),
                                                    ("config_hash", cfg_hash)))
                 and not state.get("merge_pending")
                 and not state.get("llm_pending")
                 and bool(state.get("stale")) == bool(is_stale)
                 and (analysis is None or (state.get("lint") or {}).get("sha") == pr.head_sha))
    if unchanged and not force:
        return {**result, "skipped": "unchanged", "blocking": len(state.get("titles", {})),
                "ready": bool(state.get("ready")), "blocking_titles": list(state.get("titles", {}).values())}

    if state.get("paused"):
        _check_concurrent(gh, pr, loaded, last_try)
        _fingerprint(state, pr, ci_fp, cfg_hash)
        state.update(stale=bool(is_stale), ready=False)
        body = commenter.render(pr, [], [], state, cfg)
        _publish(gh, pr, comment_id, body, state, loaded, last_try)
        commenter.sync_labels(gh, pr, cfg, {"paused"}, shared, current=gh.get_labels(pr.repo, pr.number))
        gh.set_status(pr.repo, pr.head_sha, "success", cfg["status_context"], "Paused by a maintainer")
        _reply(gh, pr, pending)
        return {**result, "paused": True, "body": body}

    # ---- lint results (computed locally only now that the PR is not skipped)
    if analysis is None and analyze_fn is not None:
        analysis = analyze_fn(pr, cfg)
    ctx = Context(pr=pr, gh=gh, cfg=cfg, workspace=workspace, shared=shared, notes=notes)
    ctx.analysis = _analysis(gh, pr, cfg, state, analysis)
    lint_pending = ctx.analysis is None and _analysis_required(cfg)

    # ---- Tier 0 (and lint results from the analysis job)
    ignored = set(state.get("ignored", []))
    findings = run_checks(ctx, skip=ignored)
    if ctx.analysis is not None:
        state["lint"] = {"sha": pr.head_sha, "notes": ctx.analysis.notes,
                         "findings": [dataclasses.asdict(f) for f in ctx.analysis.findings]}

    # ---- maintainer-resolved findings disappear; disputed ones stop blocking (a maintainer decides)
    resolved = set(state.get("ignored_keys", []))
    disputes = state.get("disputes", {})
    findings = [f for f in findings if f.key not in resolved]
    disputed_blockers = [f for f in findings if f.key in disputes and f.blocking]
    for f in findings:
        if f.key in disputes:
            f.severity = ADVISORY

    # ---- automatic fixes
    fix = apply_fixes(ctx, findings, state, force_commit=force_fix)
    ctx.notes.extend(fix.notes)
    if force_fix and not fix.pushed and not fix.suggestions and any(f.check == "lint" for f in findings):
        ctx.notes.append("`/bot fix`: no fix patch is available for this commit (the analysis results "
                         "expired or were not uploaded). Push a commit to re-run the analysis.")
    if fix.pushed:
        _check_concurrent(gh, pr, loaded, True)
        state.update(head_sha=None, config_hash=cfg_hash)  # the new head gets a fresh review
        remaining = [f for f in findings if f.key not in fix.resolved]
        body = commenter.render(pr, remaining, ctx.notes, state, cfg)
        _publish(gh, pr, comment_id, body, state, loaded, last_try)
        _reply(gh, pr, pending)
        return {**result, "fixed": sorted(fix.resolved), "pushed": True, "body": body}
    findings = [f for f in findings if f.key not in fix.resolved]
    if fix.suggestions:
        for f in findings:
            if f.check == "lint":
                f.detail += "\n\nSuggested fixes for lines in this PR are posted inline."
        findings.extend(fix.suggestions)

    # ---- Tier 1 / Tier 2 model review (budgeted), on the effective result
    triage = None
    override = bool(state.get("ready_override"))
    rc = cfg.check("llm_review")
    state["llm_pending"] = False
    if rc.get("enabled") and "llm_review" not in ignored:
        clean = override or not any(f.blocking for f in findings)
        triage, model_findings, done = _model_review(ctx, clean, state, rc)
        state["llm_pending"] = not done
        model_findings = [f for f in model_findings if f.key not in resolved]
        for f in model_findings:
            if f.key in disputes:
                if f.blocking:
                    disputed_blockers.append(f)
                f.severity = ADVISORY
        findings.extend(model_findings)

    # ---- result
    blocking = [f for f in findings if f.blocking]
    prev_titles = state.get("titles", {})
    prev_checks = state.get("title_checks", {})
    current_keys = {f.key for f in findings}
    fixed = [title for key, title in prev_titles.items()
             if key not in current_keys and key not in resolved and key not in disputes
             and prev_checks.get(key) not in ignored]
    if override:
        status, desc = "success", "Ready: a maintainer approved it for review"
    elif blocking:
        status, desc = "failure", f"{len(blocking)} issue(s) to fix before maintainer review"
    elif disputed_blockers:
        status, desc = "pending", f"Waiting for a maintainer to decide on {len(disputed_blockers)} disputed finding(s)"
    elif lint_pending:
        status, desc = "pending", "Waiting for the lint analysis of this commit"
    else:
        status, desc = "success", "Ready for maintainer review"
    state["titles"] = {f.key: f.title for f in blocking}
    state["title_checks"] = {f.key: f.check for f in blocking}
    _fingerprint(state, pr, ci_fp, cfg_hash)
    state.update(ci_pending=bool(shared.get("ci_pending", {}).get(pr.number)), lint_pending=lint_pending,
                 stale=bool(blocking) and bool(is_stale), ready=status == "success")

    # ---- publish
    _check_concurrent(gh, pr, loaded, last_try)
    inline_limit = cfg["comments"]["max_inline"]
    posted = commenter.post_inline(gh, pr, [f for f in findings if f.check != "autofix"], state, inline_limit)
    posted += commenter.post_inline(gh, pr, [f for f in findings if f.check == "autofix"], state, max(inline_limit, 10))
    body = commenter.render(pr, findings, ctx.notes, state, cfg, fixed=fixed, triage=triage)
    _publish(gh, pr, comment_id, body, state, loaded, last_try)

    wanted = set()
    if blocking and not override:
        wanted.add("needs_changes")
    if status == "success":
        wanted.add("ready")
    if any(f.check == "overlap" for f in findings):
        wanted.add("duplicate")
    if any(f.key in disputes for f in findings) and not override:
        wanted.add("disputed")
    if state["stale"]:
        wanted.add("stale")
    commenter.sync_labels(gh, pr, cfg, wanted, shared, current=gh.get_labels(pr.repo, pr.number))
    gh.set_status(pr.repo, pr.head_sha, status, cfg["status_context"], desc)
    _reply(gh, pr, pending)

    return {**result, "blocking": len(blocking), "advisory": len(findings) - len(blocking),
            "inline_posted": posted, "ready": status == "success", "status": status, "findings": findings,
            "body": body, "blocking_titles": [f.title for f in blocking]}


def _check_concurrent(gh, pr, loaded: dict, last_try: bool) -> None:
    """Before posting anything: if another run changed the command state since we read it,
    start over so every output comes from one consistent state."""
    if last_try:
        return
    _, latest = commenter.find_summary(gh, pr)
    if any(latest.get(k) != loaded.get(k) for k in COMMAND_KEYS):
        raise _Restart()


def _publish(gh, pr, comment_id, body, state, loaded, last_try) -> None:
    """Write the summary, merging in anything another run wrote in the last moments."""
    latest_id, latest = commenter.find_summary(gh, pr)
    if latest_id and latest_id != comment_id:
        comment_id = latest_id  # another run created it concurrently: edit that one
    dirty = False
    for key in COMMAND_KEYS:
        if latest.get(key) != loaded.get(key) and state.get(key) == loaded.get(key):
            # the other run changed it and we did not: keep theirs, and make the next run redo
            # the review from the merged state instead of skipping as unchanged
            if key in latest:
                state[key] = latest[key]
            else:
                state.pop(key, None)
            state["config_hash"] = None
            dirty = True
    merged = list(dict.fromkeys([*latest.get("posted_inline", []), *state.get("posted_inline", [])]))[-200:]
    if merged != state.get("posted_inline", []):
        state["posted_inline"] = merged
        dirty = True
    if dirty:
        body = commenter.replace_state(body, state)
    commenter.upsert_summary(gh, pr, comment_id, body)


def _reply(gh, pr, pending) -> None:
    for cid in pending.comment_ids:
        gh.add_reaction(pr.repo, cid, "eyes")
    for reply in pending.replies:
        gh.create_comment(pr.repo, pr.number, reply)


def _model_review(ctx: Context, clean: bool, state: dict, rc: dict) -> tuple[dict | None, list[Finding], bool]:
    """Returns (triage, findings, done). done=False means it still has to run for this commit."""
    pr, cfg, shared = ctx.pr, ctx.cfg, ctx.shared
    severity = rc.get("severity", "advisory")
    if state.get("llm_sha") == pr.head_sha:  # already reviewed this exact commit
        cached = []
        for d in state.get("llm_findings", []):
            try:
                f = Finding(**d)
            except TypeError:
                continue
            if f.key != "llm-description-mismatch":
                f.severity = BLOCKING if severity == BLOCKING else ADVISORY  # follow the current config
            cached.append(f)
        return state.get("triage"), cached, True

    if "provider" not in shared:
        shared["provider"] = llm.make_provider(cfg)
    provider = shared["provider"]
    if provider is None:
        return None, [], True  # no model configured: nothing pending
    if rc.get("only_when_clean", True) and not clean:
        ctx.notes.append("The model review runs after the blocking issues are fixed.")
        return None, [], True  # a new push or a resolved finding re-runs the review anyway
    budget = cfg["llm"].get("max_reviews_per_run", 20)
    if shared.get("llm_reviews", 0) >= budget:
        ctx.notes.append("Model review skipped: this run's review budget is used up; it will run on the next pass.")
        return None, [], False
    shared["llm_reviews"] = shared.get("llm_reviews", 0) + 1

    out: list[Finding] = []
    triage = llm.triage(provider, cfg, pr, ctx.files)
    if triage and not triage.get("description_matches_diff", True) and triage.get("mismatch_note"):
        out.append(Finding(check="llm_review", severity=ADVISORY, key="llm-description-mismatch",
                           title="The description may not match the changes",
                           detail=str(triage["mismatch_note"])[:800]))
    trivial = triage and triage.get("risk") == "low" and triage.get("change_type") in ("docs", "test", "chore")
    if trivial:
        ctx.notes.append("Low-risk change; skipped the deep model review.")
    else:
        review, skip_reason = llm.deep_review(provider, cfg, pr, ctx.files, _guidelines(ctx.gh, pr, shared))
        if skip_reason:
            ctx.notes.append(f"Deep model review skipped: {skip_reason}.")
        else:
            ctx.notes.append(f"Deep model review found {len(review)} high-confidence issue(s).")
        out.extend(review)
        if skip_reason and "did not return" in skip_reason:
            return triage, out, False  # transient failure: not cached, retried on the next run
    state["llm_sha"] = pr.head_sha
    state["triage"] = triage
    state["llm_findings"] = [dataclasses.asdict(f) for f in out]
    return triage, out, True


def sweep(gh, repo: str, *, limit: int | None = None, workspace: str | None = None, force: bool = False,
          on_result: Callable[[dict], None] | None = None,
          analyze_fn: Callable[[Any, Config], Any] | None = None) -> list[dict]:
    """Review every open PR (backlog mode). Unchanged PRs cost a few API calls each."""
    shared: dict = {}
    results = []
    for item in gh.list_open_prs(repo, limit=limit):
        try:
            res = review_pr(gh, repo, item["number"], workspace=workspace, shared=shared, force=force,
                            analyze_fn=analyze_fn)
        except Exception as e:
            log.exception("review of #%s failed", item["number"])
            res = {"repo": repo, "number": item["number"], "title": item.get("title"), "error": str(e)}
        results.append(res)
        if on_result:
            on_result(res)
    return results


def render_report(repo: str, results: list[dict]) -> str:
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    reviewed = [r for r in results if not r.get("error") and r.get("skipped") in (None, "unchanged")
                and not r.get("paused")]
    ready = [r for r in reviewed if r.get("ready")]
    needs = [r for r in reviewed if not r.get("ready") and r.get("blocking")]
    waiting = [r for r in reviewed if not r.get("ready") and not r.get("blocking")]
    skipped = [r for r in results if r.get("skipped") not in (None, "unchanged") or r.get("paused")]
    errors = [r for r in results if r.get("error")]
    lines = [f"# prbot backlog report: {repo}", "", f"Generated {when}. {len(results)} open PRs scanned.", "",
             "| Bucket | PRs |", "| --- | --- |",
             f"| Ready for maintainer review | {len(ready)} |",
             f"| Needs changes from the author | {len(needs)} |",
             f"| Waiting (lint analysis or a disputed finding) | {len(waiting)} |",
             f"| Skipped (draft, paused, bot author) | {len(skipped)} |",
             f"| Errors | {len(errors)} |", ""]
    if ready:
        lines += ["## Ready for review", ""]
        lines += [f"- #{r['number']} {r['title']} ({r['author']})"
                  + (f" — {r['advisory']} suggestion(s)" if r.get("advisory") else "") for r in ready]
        lines.append("")
    if needs:
        lines += ["## Needs changes", ""]
        for r in needs:
            lines.append(f"- #{r['number']} {r['title']} ({r['author']}): " + "; ".join(r.get("blocking_titles", [])[:3]))
        lines.append("")
    if errors:
        lines += ["## Errors", ""] + [f"- #{r['number']}: {r['error']}" for r in errors] + [""]
    return "\n".join(lines)
