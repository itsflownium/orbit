"""Lint findings from the analysis job. This module never runs tools itself; see prbot/analyzer.py."""

from __future__ import annotations

from ..models import Finding
from . import Context, check


@check("lint")
def lint(ctx: Context) -> list[Finding]:
    analysis = ctx.analysis
    if analysis is None:
        if ctx.cfg.get("execution") != "never":
            ctx.notes.append("Lint results for this commit are not available yet; they are added when "
                             "the analysis job finishes.")
        return []
    ctx.notes.extend(analysis.notes)
    sev = ctx.severity("lint")
    out = []
    for f in analysis.findings:
        # the analysis job is untrusted: severity comes from this repo's config, except that
        # results from a non-pinned tool version are advisory
        f.severity = sev if f.severity == "blocking" else "advisory"
        out.append(f)
    return out
