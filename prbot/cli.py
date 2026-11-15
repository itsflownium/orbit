"""Command line entry point.

  prbot review OWNER/REPO NUMBER   review one PR
  prbot sweep OWNER/REPO           review every open PR (backlog mode), optional markdown report
  prbot event                      GitHub Actions entry (privileged; never runs PR code)
  prbot prepare OWNER/REPO NUMBER  analysis job, step 1: PR data + git objects (read-only token)
  prbot analyze                    analysis job, step 2: run the repo's tools (no secrets)
  prbot config [FILE]              print the merged configuration
  prbot pins                       pip specs for the repo's pinned lint tools
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

import yaml

from . import __version__, analyzer
from .commands import handle_comment
from .config import parse_config
from .github import GitHub
from .runner import render_report, review_pr, sweep

log = logging.getLogger("prbot")
DEFAULT_CATCH_UP_LIMIT = 20


def _print_result(res: dict, verbose: bool) -> None:
    head = f"#{res.get('number')} {res.get('title', '')}"
    if res.get("skipped"):
        print(f"{head}: skipped ({res['skipped']})")
    elif res.get("error"):
        print(f"{head}: ERROR {res['error']}")
    elif res.get("pushed"):
        print(f"{head}: pushed fixes for {', '.join(res.get('fixed', []))}")
    else:
        status = "READY" if res.get("ready") else "WAITING" if res.get("status") == "pending" else "NEEDS CHANGES"
        waiting = "; waiting for " + ", ".join(res["waiting_on"]) if res.get("waiting_on") else ""
        print(f"{head}: {status} ({res.get('blocking', 0)} blocking, {res.get('advisory', 0)} advisory{waiting})")
    if verbose and res.get("body"):
        print("-" * 72 + "\n" + res["body"] + "\n" + "-" * 72)


def _local_analyzer(gh: GitHub, workspace: str | None, enabled: bool):
    """A callback that runs the repo's linters on this machine (local CLI use only). review_pr
    calls it only for PRs it is actually going to review (not skipped, not unchanged)."""
    if not workspace or not enabled:
        return None
    from .workspace import Workspace

    def run(pr, cfg):
        if pr.is_fork:
            log.warning("running the repo's tools on fork PR #%s on this machine; only tools that do not "
                        "execute PR code are used, but prefer a disposable clone", pr.number)
        if not Workspace(workspace, token=gh.token).fetch_pr(pr):
            return None
        try:
            return analyzer.analyze(pr, cfg, workspace)
        except analyzer.AnalysisFailed as e:
            log.warning("%s", e)
            return None
        except analyzer.WorkspaceDirty as e:
            log.error("%s", e)
            raise SystemExit(2) from None

    return run


def cmd_review(args, gh: GitHub) -> int:
    res = review_pr(gh, args.repo, args.number, workspace=args.workspace, force=args.force,
                    include_drafts=args.force or args.fresh_model, force_fix=args.fix, fresh_model=args.fresh_model,
                    analyze_fn=_local_analyzer(gh, args.workspace, not args.no_lint))
    _print_result(res, verbose=args.dry_run or args.verbose)
    return 1 if res.get("error") else 0


def cmd_sweep(args, gh: GitHub) -> int:
    results = sweep(gh, args.repo, limit=args.limit, workspace=args.workspace, force=args.force,
                    on_result=lambda r: _print_result(r, verbose=args.verbose),
                    analyze_fn=_local_analyzer(gh, args.workspace, not args.no_lint))
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write(render_report(args.repo, results))
        print(f"wrote {args.report}")
    return 1 if any(r.get("error") for r in results) else 0


def cmd_prepare(args, gh: GitHub) -> int:
    """Analysis job, step 1 (read-only token): PR data + config + git objects, no code runs."""
    data = analyzer.prepare(gh, args.repo, args.number, args.workspace or ".")
    with open(args.out, "w", encoding="utf-8", errors="surrogateescape") as fh:
        json.dump(data, fh)
    print(f"wrote {args.out} (fetched: {data['fetched']})")
    return 0 if data["fetched"] else 2


def cmd_analyze(args, gh) -> int:
    """Analysis job, step 2 (no token): run the repo's tools on the PR head, write analysis.json."""
    for var in ("GITHUB_TOKEN", "GH_TOKEN", "PRBOT_PUSH_TOKEN", "PRBOT_CHECKS_TOKEN",
                "ACTIONS_RUNTIME_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN"):
        if os.environ.get(var):
            log.error("%s is set; run `prbot analyze` in a step without secrets", var)
            return 2
    data = analyzer.read_analysis_file(args.pr_json)
    if data.get("fetched") is False:
        log.error("Could not fetch the base and head commits for analysis.")
        return 2
    pr, cfg = analyzer.load_prepared(data)
    try:
        result = analyzer.analyze(pr, cfg, args.workspace or ".")
    except (analyzer.WorkspaceDirty, analyzer.AnalysisFailed) as e:
        log.error("%s", e)
        return 2
    with open(args.out, "w", encoding="utf-8", errors="surrogateescape") as fh:
        json.dump(result, fh)
    print(f"wrote {args.out}: {len(result['findings'])} finding(s)")
    return 0


def _event_prs(gh: GitHub, repo: str, run: dict) -> list[int]:
    """Open PRs in THIS repo whose head is the workflow run's commit."""
    numbers = [p["number"] for p in run.get("pull_requests") or []
               if ((p.get("base") or {}).get("repo") or {}).get("name") == repo.split("/")[-1]
               and ((p.get("base") or {}).get("repo") or {}).get("url", "").endswith(f"/repos/{repo}")]
    if not numbers and run.get("head_sha") and run.get("head_branch"):
        head_repo = (run.get("head_repository") or {}).get("full_name")
        if head_repo and "/" in head_repo:
            head_ref = run["head_branch"]
            for item in gh.list_open_prs(repo, head=f"{head_repo.split('/', 1)[0]}:{head_ref}"):
                head = item.get("head") or {}
                if head.get("sha") == run["head_sha"] and head.get("ref") == head_ref \
                        and ((head.get("repo") or {}).get("full_name") or "").lower() == head_repo.lower():
                    numbers.append(item["number"])
    return numbers


def cmd_event(args, gh: GitHub) -> int:
    name = os.environ.get("GITHUB_EVENT_NAME", "")
    path = os.environ.get("GITHUB_EVENT_PATH")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    event = {}
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            event = json.load(fh)
    ws = args.workspace
    verbose = args.verbose or args.dry_run

    if name in ("pull_request", "pull_request_target"):
        action = event.get("action")
        if action not in ("opened", "synchronize", "reopened", "ready_for_review", "edited"):
            print(f"ignoring pull_request action {action}")
            return 0
        changes = event.get("changes") or {}
        if action == "edited" and not ({"body", "title", "base"} & set(changes)):
            return 0
        res = review_pr(gh, repo, event["pull_request"]["number"], workspace=ws, force=action == "edited")
        _print_result(res, verbose)
        return int(bool(res.get("error")))
    elif name == "issue_comment":
        res = handle_comment(gh, event, workspace=ws)
        print(json.dumps({k: v for k, v in res.items() if k not in ("body", "findings")}, default=str))
        return int(bool(res.get("error")))
    elif name == "workflow_run":
        # CI or the analysis job finished: results changed on the same commit, so always re-review
        run = event.get("workflow_run") or {}
        numbers = _event_prs(gh, repo, run)
        if not numbers:
            print(f"No open pull requests matched completed workflow run {run.get('id', 'unknown')}.")
        failed = False
        for number in numbers:
            res = review_pr(gh, repo, number, workspace=ws, force=True)
            _print_result(res, verbose)
            failed = failed or bool(res.get("error"))
        return int(failed)
    elif name in ("schedule", "workflow_dispatch"):
        inputs = event.get("inputs") or {}
        fresh_model = str(inputs.get("fresh_model", "false")).lower() == "true"
        if inputs.get("pr"):
            res = review_pr(gh, repo, int(inputs["pr"]), workspace=ws, force=True, include_drafts=True,
                            fresh_model=fresh_model)
            _print_result(res, verbose)
            return int(bool(res.get("error")))
        else:
            if fresh_model:
                log.error("fresh_model requires a pull request number")
                return 2
            try:
                limit = int(inputs.get("limit") or os.environ.get("PRBOT_SWEEP_LIMIT") or DEFAULT_CATCH_UP_LIMIT)
                if limit < 1:
                    raise ValueError
            except ValueError:
                log.error("The catch-up limit must be a positive integer")
                return 2
            results = sweep(gh, repo, limit=limit, workspace=ws, on_result=lambda r: _print_result(r, verbose))
            report = render_report(repo, results)
            summary = os.environ.get("GITHUB_STEP_SUMMARY")
            if summary:
                with open(summary, "a", encoding="utf-8") as fh:
                    fh.write(report + "\n")
            return 1 if any(r.get("error") for r in results) else 0
    else:
        print(f"prbot: nothing to do for event {name!r}")
    return 0


def cmd_config(args, gh: GitHub | None) -> int:
    text = None
    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            text = fh.read()
    print(yaml.safe_dump(dict(parse_config(text)), sort_keys=False))
    return 0


def cmd_pins(args, gh) -> int:
    from .tools import python_pins
    print(" ".join(python_pins(args.workspace or ".")))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Orbit reviews GitHub pull requests")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--dry-run", action="store_true", help="read from GitHub but write nothing; print comments")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--workspace", help="local clone of the repo, used to run linters and fixers")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("review", help="review one PR")
    r.add_argument("repo")
    r.add_argument("number", type=int)
    r.add_argument("--force", action="store_true", help="re-review even if nothing changed")
    r.add_argument("--fresh-model", action="store_true", help="request new model output for this PR")
    r.add_argument("--fix", action="store_true", help="try to commit automatic fixes")
    r.add_argument("--no-lint", action="store_true", help="do not run the repo's linters locally")

    s = sub.add_parser("sweep", help="review all open PRs")
    s.add_argument("repo")
    s.add_argument("--limit", type=int)
    s.add_argument("--force", action="store_true")
    s.add_argument("--report", help="write a markdown backlog report to this file")
    s.add_argument("--no-lint", action="store_true", help="do not run the repo's linters locally")

    pp = sub.add_parser("prepare", help="analysis job step 1: fetch PR data and git objects")
    pp.add_argument("repo")
    pp.add_argument("number", type=int)
    pp.add_argument("--out", default="pr.json")

    an = sub.add_parser("analyze", help="analysis job step 2 (no secrets): run the repo's tools")
    an.add_argument("--pr-json", default="pr.json")
    an.add_argument("--out", default=analyzer.ARTIFACT_FILE)

    sub.add_parser("event", help="GitHub Actions entry point")

    sub.add_parser("pins", help="print pip specs for the repo's pinned lint tools (uses --workspace)")

    c = sub.add_parser("config", help="print the merged config")
    c.add_argument("file", nargs="?")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    if args.cmd == "config":
        return cmd_config(args, None)
    if args.cmd == "pins":
        return cmd_pins(args, None)
    if args.cmd == "analyze":
        return cmd_analyze(args, None)
    gh = GitHub(dry_run=args.dry_run, checks_token=os.environ.get("PRBOT_CHECKS_TOKEN"))
    if not gh.token:
        log.warning("no GITHUB_TOKEN/GH_TOKEN set; only public repos can be read and nothing can be written")
    handler = {"review": cmd_review, "sweep": cmd_sweep, "event": cmd_event, "prepare": cmd_prepare}[args.cmd]
    return handler(args, gh)


if __name__ == "__main__":
    sys.exit(main())
