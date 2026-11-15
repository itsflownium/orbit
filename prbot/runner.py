"""Orchestrates one PR review: commands -> checks -> fixes -> model review -> comment/labels/status.

This runs in the privileged context and never executes PR code. Lint results come from the
analysis job (prbot/analyzer.py), either passed in or downloaded as an artifact.

Outcomes, and the commit status each one sets:
  blocking findings                         -> failure  + bot:needs-changes
  disputed blockers or required checks pending -> pending  (no ready label)
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
from .checks.overlap import overlap_fingerprint
from .config import Config, autofix_mode, load_config, parse_config
from .diffutil import glob_match
from .fixer import apply_fixes, can_push
from .models import ADVISORY, BLOCKING, Finding

log = logging.getLogger(__name__)

# state keys that /bot commands set; a concurrent change to any of them restarts the review
COMMAND_KEYS = ("disputes", "ignored", "ignored_keys", "paused", "ready_override", "cmd_seen")
FINGERPRINT_KEYS = ("head_sha", "base_ref", "base_sha", "mergeable_state", "ci_fp", "overlap_fp", "config_hash")


class _Restart(Exception):
    pass


def _now() -> float:
    return time.time()


def _config_hash(cfg: Config) -> str:
    inputs = (cfg, llm.REVIEW_CACHE_VERSION)
    return hashlib.sha1(json.dumps(inputs, sort_keys=True, default=str).encode()).hexdigest()[:10]


def _guidelines(gh, pr, shared: dict) -> str | None:
    key = ("guidelines", pr.repo, pr.base_ref, pr.base_sha)
    if key not in shared:
        text = None
        for path in ("CONTRIBUTING.md", ".github/CONTRIBUTING.md", "docs/CONTRIBUTING.md", "AGENTS.md", "AGENTS.md"):
            text = gh.get_file_text(pr.repo, path, pr.base_ref)
            if text:
                break
        shared[key] = text
    return shared[key]


def _load_cfg(gh, pr, shared: dict, notes: list[str]) -> tuple[Config, bool]:
    key = ("config", pr.repo, pr.base_ref, pr.base_sha)
    err_key = ("config_error", pr.repo, pr.base_ref, pr.base_sha)
    if key not in shared:
        try:
            shared[key] = load_config(gh, pr.repo, pr.base_ref)
        except ValueError as e:  # keep reporting checks but don't mark the PR ready
            log.warning("invalid prereview config: %s", e)
            shared[key] = parse_config("autofix: {mode: comment}\nllm: {provider: none}")
            shared[err_key] = str(e)
    if shared.get(err_key):
        notes.append(f"Fix the repository's prereview config: {shared[err_key]}.")
    return shared[key], err_key not in shared


def _analysis_required(cfg: Config) -> bool:
    a = cfg.get("analysis", {})
    return bool(a.get("required", True) and a.get("artifact", True)
                and cfg.check_enabled("lint") and cfg.get("execution") != "never")


def _analysis(gh, pr, cfg: Config, state: dict, given: Any, force: bool) -> analyzer.Analysis | None:
    """Lint results for this head: passed in, cached in state, or downloaded."""
    if given is not None:
        a = given if isinstance(given, analyzer.Analysis) else analyzer.load_analysis(given, pr)
        if a is not None and a.head_sha == pr.head_sha:
            return a
    if force:
        latest = _download(gh, pr, cfg)
        if latest is not None:
            return latest
    cached = state.get("lint") or {}
    if cached.get("sha") == pr.head_sha and cached.get("base_sha") == pr.base_sha:
        data = {"version": analyzer.VERSION, "repo": pr.repo, "number": pr.number, "head_sha": pr.head_sha,
                "findings": cached.get("findings", []), "notes": cached.get("notes", []), "patch": ""}
        a = analyzer.load_analysis(data, pr)
        if force or (a is not None and not a.findings):
            return a
        # findings exist: the patch is needed for fixes, so try the artifact, else use the cache
        return _download(gh, pr, cfg) or a
    return None if force else _download(gh, pr, cfg)


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
    state.update(head_sha=pr.head_sha, base_ref=pr.base_ref, base_sha=pr.base_sha, mergeable_state=pr.mergeable_state,
                 ci_fp=ci_fp, config_hash=cfg_hash,
                 merge_pending=pr.mergeable is None or pr.mergeable_state == "unknown")


def review_pr(gh, repo: str, number: int, *, workspace: str | None = None, shared: dict | None = None,
              force: bool = False, include_drafts: bool = False, force_fix: bool = False, fresh_model: bool = False,
              analysis: Any = None, analyze_fn: Callable[[Any, Config], Any] | None = None,
              mutate_state: Callable[[dict], None] | None = None) -> dict[str, Any]:
    """Review one PR. If another run changes the PR's command state meanwhile, start over so the
    comment, status and labels always come from one consistent state."""
    shared = shared if shared is not None else {}
    for attempt in range(3):
        try:
            return _review_once(gh, repo, number, workspace=workspace, shared=shared, force=force or fresh_model,
                                include_drafts=include_drafts, force_fix=force_fix, fresh_model=fresh_model, analysis=analysis,
                                analyze_fn=analyze_fn, mutate_state=mutate_state, last_try=attempt == 2)
        except _Restart:
            log.info("PR #%s changed during the review; starting over", number)
    return {"repo": repo, "number": number, "skipped": "PR changed during each attempt"}


def _review_once(gh, repo, number, *, workspace, shared, force, include_drafts, force_fix, fresh_model, analysis,
                 analyze_fn, mutate_state, last_try) -> dict[str, Any]:
    pr = gh.get_pr(repo, number, with_files=False)
    result: dict[str, Any] = {"repo": repo, "number": number, "title": pr.title, "author": pr.author}
    if pr.raw.get("state", "open") != "open":
        return {**result, "skipped": "closed"}

    notes: list[str] = []
    cfg, cfg_valid = _load_cfg(gh, pr, shared, notes)
    if not cfg.get("enabled", True):
        return {**result, "skipped": "disabled by config"}
    if pr.author in cfg.get("skip_authors", []):
        return {**result, "skipped": "author skipped"}

    comments = gh.issue_comments(pr.repo, pr.number)
    comment_id, state = commenter.summary_in(gh, comments)
    previous_body = next((c.get("body") for c in comments if c.get("id") == comment_id), None)
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
    ctx = Context(pr=pr, gh=gh, cfg=cfg, workspace=workspace, shared=shared, notes=notes)
    if not cfg_valid:
        ctx.pending_checks.add("config")
    overlap_fp = ""
    ignored = set(state.get("ignored", []))
    if cfg.check_enabled("overlap") and "overlap" not in ignored and not state.get("paused"):
        try:
            overlap_fp = overlap_fingerprint(ctx)
        except Exception:
            overlap_fp = None  # run_checks reports the failure and keeps a required check pending
    if state.get("head_sha") != pr.head_sha:
        state["head_since"] = _now()
    stale_days = cfg["comments"].get("stale_days") or 0
    is_stale = bool(state.get("titles")) and bool(stale_days) and \
        _now() - state.get("head_since", _now()) > stale_days * 86400

    unchanged = (all(state.get(k) == v for k, v in (("head_sha", pr.head_sha), ("base_ref", pr.base_ref),
                                                    ("base_sha", pr.base_sha),
                                                    ("mergeable_state", pr.mergeable_state), ("ci_fp", ci_fp),
                                                    ("overlap_fp", overlap_fp),
                                                    ("config_hash", cfg_hash)))
                 and not state.get("merge_pending")
                 and not state.get("lint_pending")
                 and not state.get("pending_checks")
                 and not state.get("llm_pending")
                 and bool(state.get("stale")) == bool(is_stale)
                 and (analysis is None or (state.get("lint") or {}).get("sha") == pr.head_sha))
    if unchanged and not (force or force_fix):
        return {**result, "skipped": "unchanged", "blocking": len(state.get("titles", {})),
                "ready": bool(state.get("ready")), "paused": bool(state.get("paused")),
                "blocking_titles": list(state.get("titles", {}).values())}

    if state.get("paused"):
        _check_concurrent(gh, pr, loaded, last_try)
        _fingerprint(state, pr, ci_fp, cfg_hash)
        state.update(stale=bool(is_stale), ready=False, lint_pending=False, llm_pending=False,
                     pending_checks=[], overlap_fp=overlap_fp)
        commenter.sync_labels(gh, pr, cfg, {"paused"}, shared, current=gh.get_labels(pr.repo, pr.number))
        _set_status(gh, pr, cfg, state, "pending", "Paused by a maintainer", force=force)
        body = commenter.render(pr, [], [], state, cfg)
        _publish(gh, pr, comment_id, body, state, loaded, last_try)
        _reply(gh, pr, pending)
        return {**result, "paused": True, "body": body}

    # ---- lint results (computed locally only now that the PR is not skipped)
    pr.files = gh.get_pr_files(repo, number)
    if analysis is None and analyze_fn is not None and "lint" not in ignored:
        analysis = analyze_fn(pr, cfg)
    # ---- Tier 0 (and lint results from the analysis job)
    if "lint" not in ignored and cfg.check_enabled("lint"):
        ctx.analysis = _analysis(gh, pr, cfg, state, analysis, force or force_fix)
    lint_pending = "lint" not in ignored and ctx.analysis is None and _analysis_required(cfg)
    findings = run_checks(ctx, skip=ignored)
    if ctx.analysis is not None:
        state["lint"] = {"sha": pr.head_sha, "base_sha": pr.base_sha, "notes": ctx.analysis.notes,
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
    fix_mode = autofix_mode(cfg)
    if fix_mode != "comment" and (fix_mode == "commit" or force_fix) and can_push(ctx):
        _check_concurrent(gh, pr, loaded, last_try)
    fix = apply_fixes(ctx, findings, state, force_commit=force_fix)
    ctx.notes.extend(fix.notes)
    if force_fix and fix_mode == "comment":
        ctx.notes.append("This repository uses comment-only mode. Push your fixes to the PR branch.")
    elif force_fix and not fix.pushed and not fix.suggestions and any(f.check == "lint" for f in findings):
        ctx.notes.append("`/bot fix`: no fix patch is available for this commit (the analysis results "
                         "expired or were not uploaded). Push a commit to re-run the analysis.")
    if fix.pushed:
        state.update(head_sha=None, config_hash=cfg_hash, ready=False, fix_pending=True)
        body = commenter.render(pr, findings, ctx.notes, state, cfg)
        _publish(gh, pr, comment_id, body, state, loaded, last_try)
        commenter.sync_labels(gh, pr, cfg, set(), shared, current=gh.get_labels(pr.repo, pr.number))
        _reply(gh, pr, pending)
        return {**result, "fixed": sorted(fix.resolved), "pushed": True, "body": body}
    findings = [f for f in findings if f.key not in fix.resolved]
    findings.extend(fix.suggestions)

    # ---- Tier 1 / Tier 2 model review (budgeted), on the effective result
    triage = None
    override = bool(state.get("ready_override"))
    rc = cfg.check("llm_review")
    state["llm_pending"] = False
    if rc.get("enabled") and "llm_review" not in ignored:
        clean = override or not (lint_pending or ctx.pending_checks or any(f.blocking for f in findings))
        triage, model_findings, done = _model_review(ctx, clean, state, rc, fresh=fresh_model)
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
        if ctx.pending_checks:
            status, desc = "pending", "Waiting for required checks to finish"
        elif state["llm_pending"] and rc.get("severity") == BLOCKING:
            status, desc = "pending", "Waiting for the model review"
    state["titles"] = {f.key: f.title for f in blocking}
    state["title_checks"] = {f.key: f.check for f in blocking}
    _fingerprint(state, pr, ci_fp, cfg_hash)
    state.pop("fix_pending", None)
    state.update(ci_pending="ci" in ctx.pending_checks, lint_pending=lint_pending,
                 overlap_fp=overlap_fp, dispute_pending=bool(disputed_blockers),
                 pending_checks=sorted(ctx.pending_checks),
                 stale=bool(blocking) and bool(is_stale), ready=status == "success")

    # ---- publish
    _check_concurrent(gh, pr, loaded, last_try)
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
    _set_status(gh, pr, cfg, state, status, desc, force=force)
    inline_limit = cfg["comments"]["max_inline"]
    posted = commenter.post_inline(gh, pr, [f for f in findings if f.check != "autofix"], state, inline_limit,
                                   allow_suggestions=fix_mode != "comment")
    posted += commenter.post_inline(gh, pr, [f for f in findings if f.check == "autofix"], state, max(inline_limit, 10))
    body = commenter.render(pr, findings, ctx.notes, state, cfg, fixed=fixed, triage=triage)
    if body != previous_body:
        _publish(gh, pr, comment_id, body, state, loaded, last_try)
    _reply(gh, pr, pending)

    waiting_on = sorted(ctx.pending_checks)
    if lint_pending:
        waiting_on.append("lint analysis")
    if state["llm_pending"]:
        waiting_on.append("model review")
    if disputed_blockers:
        waiting_on.append("maintainer decision")
    return {**result, "blocking": len(blocking), "advisory": len(findings) - len(blocking),
            "inline_posted": posted, "ready": status == "success", "status": status, "waiting_on": waiting_on,
            "findings": findings,
            "body": body, "blocking_titles": [f.title for f in blocking]}


def _check_concurrent(gh, pr, loaded: dict, last_try: bool) -> None:
    """Before posting anything: if another run changed the command state since we read it,
    start over so every output comes from one consistent state."""
    latest_pr = gh.get_pr(pr.repo, pr.number, with_files=False)
    if (latest_pr.head_sha, latest_pr.base_sha, latest_pr.base_ref, latest_pr.maintainer_can_modify,
        latest_pr.raw.get("state", "open"), latest_pr.draft) != (
        pr.head_sha, pr.base_sha, pr.base_ref, pr.maintainer_can_modify, "open", pr.draft
    ):
        raise _Restart()
    _, latest = commenter.find_summary(gh, pr)
    if not last_try and any(latest.get(k) != loaded.get(k) for k in COMMAND_KEYS + FINGERPRINT_KEYS):
        raise _Restart()


def _set_status(gh, pr, cfg, state, status, description, *, force):
    report = [pr.head_sha, cfg["status_context"], status, description]
    if force or report != state.get("reported_status"):
        gh.set_status(pr.repo, pr.head_sha, status, cfg["status_context"], description)
        state["reported_status"] = report


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


def _model_review(ctx: Context, clean: bool, state: dict, rc: dict, *, fresh: bool) -> tuple[dict | None, list[Finding], bool]:
    """Returns (triage, findings, done). done=False means it still has to run for this commit."""
    pr, cfg, shared = ctx.pr, ctx.cfg, ctx.shared
    severity = rc.get("severity", "advisory")
    inputs = [pr.base_ref, pr.title, pr.body, cfg["llm"], rc, llm.TRIAGE_SYSTEM, llm.REVIEW_SYSTEM,
              llm.REVIEW_CACHE_VERSION]
    model_fp = hashlib.sha1(json.dumps(inputs, sort_keys=True, default=str).encode()).hexdigest()[:12]
    if fresh:
        # A failed fresh review must stay pending instead of falling back to an older cached answer.
        for key in ("llm_sha", "llm_fp", "llm_findings", "triage"):
            state.pop(key, None)
    if state.get("llm_sha") == pr.head_sha and state.get("llm_fp") == model_fp:
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

    provider_key = ("provider", _config_hash(cfg))
    if provider_key not in shared:
        shared[provider_key] = llm.make_provider(cfg)
    provider = shared[provider_key]
    if provider is None:
        if llm.resolve_provider_name(cfg) is not None:
            ctx.notes.append("The configured model provider could not start. Check the endpoint and credentials.")
            return None, [], False
        return None, [], True  # no model configured: nothing pending
    if rc.get("only_when_clean", True) and not clean:
        ctx.notes.append("The model review runs after the blocking issues are fixed and required checks pass.")
        return None, [], True  # a new push or a resolved finding re-runs the review anyway
    budget = cfg["llm"].get("max_reviews_per_run", 20)
    if shared.get("llm_reviews", 0) >= budget:
        ctx.notes.append("Model review skipped: this run's review budget is used up; it will run on the next pass.")
        return None, [], False
    shared["llm_reviews"] = shared.get("llm_reviews", 0) + 1
    log.info("Requesting %smodel review for PR #%s with %s (%s)", "fresh " if fresh else "",
             pr.number, llm.model_for(cfg, "review_model"), llm.resolve_provider_name(cfg))
    if fresh:
        ctx.notes.append("A fresh model review was requested for this commit.")

    out: list[Finding] = []
    triage = llm.triage(provider, cfg, pr, ctx.files)
    if triage and not triage.get("description_matches_diff", True) and triage.get("mismatch_note"):
        out.append(Finding(check="llm_review", severity=ADVISORY, key="llm-description-mismatch",
                           title="The description may not match the changes",
                           detail=str(triage["mismatch_note"])[:800]))
    tc = cfg.check("tests")
    code_exts = tuple(tc.get("code_extensions", ()))
    test_paths = tc.get("test_patterns", [])
    has_product_code = any(f.filename.endswith(code_exts) and not glob_match(f.filename, test_paths)
                           for f in ctx.files)
    trivial = (triage and triage.get("risk") == "low" and triage.get("description_matches_diff")
               and triage.get("change_type") in ("docs", "test", "chore") and not has_product_code)
    if trivial:
        ctx.notes.append("Low-risk change; skipped the deep model review.")
    else:
        review, skip_reason = llm.deep_review(provider, cfg, pr, ctx.files, _guidelines(ctx.gh, pr, shared), gh=ctx.gh)
        if skip_reason:
            ctx.notes.append(f"Deep model review skipped: {skip_reason}.")
        else:
            ctx.notes.append(f"Deep model review found {len(review)} high-confidence issue(s).")
        out.extend(review)
        if skip_reason and "did not return" in skip_reason:
            return triage, out, False  # transient failure: not cached, retried on the next run
    state["llm_sha"] = pr.head_sha
    state["llm_fp"] = model_fp
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
    pending = [r for r in reviewed if r.get("waiting_on")]
    skipped = [r for r in results if r.get("skipped") not in (None, "unchanged") or r.get("paused")]
    errors = [r for r in results if r.get("error")]
    lines = [f"# prbot backlog report: {repo}", "", f"Generated {when}. {len(results)} open PRs scanned.", "",
             "| Bucket | PRs |", "| --- | --- |",
             f"| Ready for maintainer review | {len(ready)} |",
             f"| Needs changes from the author | {len(needs)} |",
             f"| Waiting for checks or a decision | {len(waiting)} |",
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
    if pending:
        lines += ["## Still waiting", ""]
        lines += [f"- #{r['number']} {commenter.clean(r['title'])}: " + ", ".join(r["waiting_on"]) for r in pending]
        lines.append("")
    if errors:
        lines += ["## Errors", ""] + [f"- #{r['number']}: {r['error']}" for r in errors] + [""]
    return "\n".join(lines)
