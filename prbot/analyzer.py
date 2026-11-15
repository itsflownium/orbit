"""Running the repo's linters/formatters on PR code, isolated from the bot's secrets.

Security model
--------------
PR code (and anything it can influence, such as tool configs in the PR head) only ever
runs inside `prbot analyze`, which the workflow runs in a separate `pull_request` job with
a read-only token, no secrets, and no network calls of its own. Its output, an
"analysis" JSON file uploaded as a workflow artifact, is untrusted data.

The privileged bot (`prbot event`) never executes PR code. It downloads the artifact,
checks it belongs to the PR's current head commit, keeps only lint findings and only
patch hunks that touch lines the PR itself adds (recomputed from the GitHub API, not
taken from the artifact), and escapes all text before posting it.

Flow inside the analysis job:
  prbot prepare   (has a read-only token) -> fetches PR metadata, config and git objects
  prbot analyze   (no token in its env)   -> detects tools on the BASE checkout, checks out
                                             the PR head, runs tools, writes analysis.json
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

from . import patches
from .config import Config, parse_config_dict
from .diffutil import added_lines
from .models import ADVISORY, BLOCKING, ChangedFile, Finding, PullRequest
from .tools import LINT, Tool, detect_tools
from .workspace import Workspace, run

log = logging.getLogger(__name__)

VERSION = 1
ARTIFACT_NAME = "prbot-analysis"
ARTIFACT_FILE = "analysis.json"
MAX_PATCH_BYTES = 1_000_000


class WorkspaceDirty(RuntimeError):
    pass


# --------------------------------------------------------------------------- prepare

def prepare(gh, repo: str, number: int, workspace: str) -> dict[str, Any]:
    """Everything `analyze` needs, gathered while a (read-only) token is available."""
    from .config import load_config

    pr = gh.get_pr(repo, number)
    cfg = load_config(gh, repo, pr.base_ref)
    fetched = Workspace(workspace, token=gh.token).fetch_pr(pr)
    return {
        "version": VERSION,
        "pr": pr.raw,
        "files": [dataclasses.asdict(f) for f in pr.files],
        "config": dict(cfg),
        "fetched": fetched,
    }


def load_prepared(data: dict[str, Any]) -> tuple[PullRequest, Config]:
    pr = PullRequest.from_api(data["pr"])
    pr.files = [ChangedFile(**f) for f in data.get("files", [])]
    return pr, parse_config_dict(data.get("config") or {})


# --------------------------------------------------------------------------- analyze

@dataclass
class _Ctx:
    pr: PullRequest
    cfg: Config
    workspace: str
    notes: list[str] = field(default_factory=list)

    @property
    def files(self) -> list[ChangedFile]:
        from .diffutil import glob_match
        ignore = self.cfg.get("ignore_paths", [])
        return [f for f in self.pr.files if not glob_match(f.filename, ignore)]

    def author_lines(self) -> dict[str, dict[int, str]]:
        return {f.filename: added_lines(f.patch) for f in self.files if f.patch}


def _files_for(tool: Tool, ctx: _Ctx) -> list[str]:
    out = []
    for f in ctx.files:
        name = f.filename
        if f.status == "removed" or not name.endswith(tool.exts) or name.startswith("-"):
            continue
        root = os.path.realpath(ctx.workspace)
        full = os.path.join(root, name)
        if os.path.islink(full) or not os.path.realpath(full).startswith(root + os.sep):
            continue  # never let a formatter follow a symlink out of the checkout
        out.append(name)
    return out


def _fixer_patches(ctx: _Ctx, tools: list[Tool], keep: set[str]) -> list[patches.FilePatch]:
    ws = Workspace(ctx.workspace)
    for tool in tools:
        files = _files_for(tool, ctx)
        for cmd in tool.fix_cmds(files) if files else []:
            run(cmd, ctx.workspace, timeout=300)
    diff = ws.diff([f.filename for f in ctx.files])
    ws.reset(keep_untracked=keep)
    return patches.keep_author_hunks(diff, ctx.author_lines())


def _lint(ctx: _Ctx, tools: list[Tool], keep: set[str], severity: str) -> tuple[list[Finding], list[Tool]]:
    out: list[Finding] = []
    with_findings: list[Tool] = []
    mine = ctx.author_lines()
    added_files = {f.filename for f in ctx.files if f.status == "added"}
    ran = []
    for tool in tools:
        files = _files_for(tool, ctx)
        if not files:
            continue
        ran.append(tool.version_note or f"`{tool.name}`")
        if tool.kind == LINT:
            result = run(tool.check_cmd(files), ctx.workspace, timeout=300)
            Workspace(ctx.workspace).reset(keep_untracked=keep)
            if result.code == 0:
                continue
            issues = [(p, ln, msg) for p, ln, msg in tool.parse(result.out, files, ctx.workspace)
                      if (ln in mine.get(p, set())) or (ln is None and p in added_files)]
            if not issues:
                if result.code in (124, 127):
                    ctx.notes.append(f"`{tool.name}` could not run: {result.out.strip()[:200]}")
                continue
        else:
            fps = _fixer_patches(ctx, [tool], keep)
            issues = [(fp.path, h.old_start or 1, "not formatted") for fp in fps for h in fp.hunks]
            if not issues:
                continue
        with_findings.append(tool)
        shown = "\n".join(f"- `{p}` line {ln}: {msg}" for p, ln, msg in issues[:20])
        more = f"\n- ...and {len(issues) - 20} more" if len(issues) > 20 else ""
        nfiles = len({p for p, _, _ in issues})
        what = "issue(s)" if tool.kind == LINT else "unformatted change(s)"
        cmd = " ".join(tool.fix_cmds(["<files>"])[0])
        out.append(Finding(
            check="lint", severity=severity if tool.exact_version else ADVISORY, key=f"lint-{tool.name}",
            fixable=True, title=f"`{tool.name}` reports {len(issues)} {what} in {nfiles} file(s)",
            detail=f"Only lines added by this PR are reported.\n\n{shown}{more}\n\nFix locally:\n\n```bash\n{cmd}\n```",
        ))
    if ran:
        ctx.notes.append("Ran " + ", ".join(ran) + " on the changed files.")
    return out, with_findings


def _restore_point(ws: Workspace) -> str:
    branch = ws.git_out("symbolic-ref", "-q", "--short", "HEAD").strip()
    return branch or ws.head()


def analyze(pr: PullRequest, cfg: Config, workspace: str) -> dict[str, Any]:
    """Run the repo's enforced tools on the PR. Offline: needs `prepare` (or a fetch) first."""
    result: dict[str, Any] = {"version": VERSION, "repo": pr.repo, "number": pr.number,
                              "head_sha": pr.head_sha, "findings": [], "patch": "", "notes": []}
    policy = cfg.get("execution", "same-repo")
    if policy == "never" or not cfg.check_enabled("lint"):
        return result
    ws = Workspace(workspace)
    if not ws.is_repo():
        result["notes"].append("No git checkout available for linting.")
        return result
    if ws.has_credentials():
        raise WorkspaceDirty(f"{ws.path}/.git/config holds credentials; check out with persist-credentials: false")
    status = ws.git_out("status", "--porcelain", "--untracked-files=no").strip()
    if status:
        raise WorkspaceDirty(f"{ws.path} has uncommitted changes; use a dedicated clone for --workspace")
    restore = _restore_point(ws)
    try:
        # 1. Decide tools from the BASE branch, so the PR cannot pick them.
        base_rev = f"refs/prbot/base-{pr.number}"
        if not ws.git_out("rev-parse", "--verify", "--quiet", base_rev).strip() or not ws.checkout(base_rev):
            # not fetched (e.g. the workflow checked out the base branch itself): use what is checked out
            base_rev = restore
            ws.checkout(base_rev)
        lc = cfg.check("lint")
        tools = detect_tools(ws.path, lc.get("tools", "auto"), lc.get("require_enforcement", True))
        if policy != "always" and pr.is_fork:
            tools = [t for t in tools if t.safe_untrusted]
        if not tools:
            return result
        # 2. Run them on the PR head.
        if not ws.checkout(pr.head_sha):
            result["notes"].append("Could not check out the PR head for linting.")
            return result
        keep = ws.untracked()
        ctx = _Ctx(pr=pr, cfg=cfg, workspace=ws.path)  # ws.path is already realpath'd
        findings, with_findings = _lint(ctx, tools, keep, cfg.check("lint").get("severity", BLOCKING))
        if findings:
            fps = _fixer_patches(ctx, with_findings, keep)
            text = patches.render(fps)
            if len(text.encode("utf-8", "surrogateescape")) <= MAX_PATCH_BYTES:
                result["patch"] = text
        result["findings"] = [dataclasses.asdict(f) for f in findings]
        result["notes"] = ctx.notes
        result["tools"] = [t.name for t in with_findings]
        return result
    finally:
        ws.reset()
        if ws.git("checkout", "--force", "-q", restore).code != 0:
            ws.checkout(restore)


# --------------------------------------------------------------------------- loading (privileged side)

@dataclass
class Analysis:
    head_sha: str
    findings: list[Finding]
    file_patches: list[patches.FilePatch]
    notes: list[str]
    tools: list[str]


def _s(value: Any, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


_KEY = re.compile(r"^lint-[a-z0-9][a-z0-9-]{0,47}$")


def load_analysis(data: Any, pr: PullRequest, ignore_paths: list[str] | None = None) -> Analysis | None:
    """Validate an untrusted analysis result against the live PR. None if it does not apply."""
    if not isinstance(data, dict) or data.get("version") != VERSION:
        return None
    if data.get("repo") != pr.repo or data.get("number") != pr.number or data.get("head_sha") != pr.head_sha:
        return None
    findings: list[Finding] = []
    for item in (data.get("findings") or [])[:20]:
        if not isinstance(item, dict):
            continue
        key = _s(item.get("key"), 64)
        if not _KEY.match(key) or any(f.key == key for f in findings):
            continue
        findings.append(Finding(
            check="lint", key=key,
            severity=BLOCKING if item.get("severity") == BLOCKING else ADVISORY,
            title=_s(item.get("title"), 200) or "Lint issue", detail=_s(item.get("detail"), 6000), fixable=True,
        ))
    author = {f.filename: added_lines(f.patch) for f in pr.files if f.patch}
    patch_text = _s(data.get("patch"), MAX_PATCH_BYTES)
    fps = patches.keep_author_hunks(patch_text, author) if patch_text else []
    notes = [_s(n, 300) for n in (data.get("notes") or [])[:10] if isinstance(n, str)]
    tools = [_s(t, 40) for t in (data.get("tools") or [])[:10] if isinstance(t, str)]
    return Analysis(pr.head_sha, findings, fps, notes, tools)


def read_analysis_file(path: str) -> Any:
    with open(path, encoding="utf-8", errors="surrogateescape") as fh:
        return json.load(fh)


def fetch_analysis(gh, pr: PullRequest, workflow_path: str | None = None) -> "Analysis | None":
    """The analysis built for the PR's head commit by the analysis workflow, if any.
    Every candidate artifact is tried, so a stray one for the same commit cannot hide it."""
    for art in gh.analysis_artifacts(pr.repo, ARTIFACT_NAME, pr, workflow_path):
        try:
            files = gh.download_artifact(art)
        except Exception as e:  # API error, bad zip
            log.info("could not download artifact %s: %s", art.get("id"), e)
            continue
        if not files or ARTIFACT_FILE not in files:
            continue
        try:
            data = json.loads(files[ARTIFACT_FILE].decode("utf-8", "surrogateescape"))
        except ValueError:
            continue
        loaded = load_analysis(data, pr)
        if loaded is not None:
            return loaded
    return None
