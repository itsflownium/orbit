"""Tier 0 checks: deterministic, no model, cheap.

Each check is a function ``(ctx) -> list[Finding]`` registered with ``@check``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import Config
from ..diffutil import glob_match
from ..models import BLOCKING, ChangedFile, Finding, PullRequest

log = logging.getLogger(__name__)

CheckFn = Callable[["Context"], list[Finding]]
REGISTRY: dict[str, CheckFn] = {}


def check(name: str) -> Callable[[CheckFn], CheckFn]:
    def wrap(fn: CheckFn) -> CheckFn:
        REGISTRY[name] = fn
        return fn
    return wrap


@dataclass
class Context:
    pr: PullRequest
    gh: Any  # prbot.github.GitHub
    cfg: Config
    workspace: str | None = None  # local checkout of the PR head, if any
    shared: dict[str, Any] = field(default_factory=dict)  # caches shared across PRs in a sweep
    analysis: Any = None  # prbot.analyzer.Analysis for this head commit, if available
    notes: list[str] = field(default_factory=list)  # info lines for the summary comment
    pending_checks: set[str] = field(default_factory=set)

    def severity(self, name: str) -> str:
        return self.cfg.check(name).get("severity", "advisory")

    @property
    def files(self) -> list[ChangedFile]:
        """Changed files, minus globally ignored paths."""
        ignore = self.cfg.get("ignore_paths", [])
        return [f for f in self.pr.files if not glob_match(f.filename, ignore)]


def run_checks(ctx: Context, names: list[str] | None = None, skip: set[str] | None = None) -> list[Finding]:
    from . import basic, lint, overlap  # noqa: F401  (registers checks)

    findings: list[Finding] = []
    for name, fn in REGISTRY.items():
        if names and name not in names:
            continue
        if skip and name in skip:
            continue
        if not ctx.cfg.check_enabled(name):
            continue
        try:
            findings.extend(fn(ctx))
        except Exception as e:  # one broken check must not stop the review
            log.exception("check %s failed", name)
            ctx.notes.append(f"Check `{name}` could not run: {type(e).__name__}: {e}")
            if ctx.severity(name) == BLOCKING:
                ctx.pending_checks.add(name)
    return findings
