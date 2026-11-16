"""Select bounded architecture context for a pull request from the trusted base tree."""

from __future__ import annotations

import json
from pathlib import PurePosixPath

from .diffutil import glob_match
from .github import GitHubError, cached_tree


_ENTRY_POINTS = {"api", "app", "cli", "client", "gateway", "handlers", "index", "main",
                 "router", "routes", "runner", "server"}
_PROJECT_FOLDERS = {"apps", "lib", "packages", "services", "src"}
_APP_FOLDERS = {"api", "backend", "client", "desktop", "frontend", "gateway", "server", "ui", "web"}
_OVERVIEWS = ("ARCHITECTURE.md", "docs/architecture.md", "README.md")


def _area(path: str) -> str:
    parts = path.split("/")
    if len(parts) == 1:
        return "."
    if len(parts) > 2 and parts[0] in _PROJECT_FOLDERS:
        return "/".join(parts[:2])
    return parts[0]


def architecture_context(gh, cfg: dict, pr, files, shared: dict) -> tuple[str, list[str]]:
    """Return a layout overview and related files to read at the PR head."""
    if not gh or not pr.base_sha:
        return "", []
    entries, complete = cached_tree(gh, pr.repo, pr.base_sha, shared)
    if not entries:
        return "", []
    ignored = cfg.get("ignore_paths", [])
    paths = {p for p, (mode, _) in entries.items() if mode in ("100644", "100755")
             and len(p) <= 220 and not glob_match(p, ignored)}
    changed = {f.filename for f in files}
    changed_areas = {_area(p) for p in changed}
    stems = {PurePosixPath(p).stem.lower() for p in changed} - _ENTRY_POINTS
    extensions = tuple(cfg["checks"].get("tests", {}).get("code_extensions") or ()) + (".svelte", ".vue")
    entries_by_area: dict[str, str] = {}
    related = []
    for path in sorted(paths):
        if not path.endswith(extensions):
            continue
        source = PurePosixPath(path)
        area = _area(path)
        stem = source.stem.lower()
        entry = stem in _ENTRY_POINTS
        # A nearby entry point or a same-named module across a service boundary is useful context.
        if entry and (area in changed_areas or _APP_FOLDERS.intersection(source.parts[:-1])):
            entries_by_area.setdefault(area, path)
        if path not in changed and (stem in stems or (entry and area in changed_areas)):
            related.append((stem not in stems, area not in changed_areas, len(source.parts), path))
    related.sort()
    candidate_paths = [item[-1] for item in related[:2]]
    boundary = next((path for area, path in entries_by_area.items()
                     if area not in changed_areas and path not in changed), None)
    if boundary and boundary not in candidate_paths:
        candidate_paths.append(boundary)
    summary = {
        "changed_areas": sorted(changed_areas)[:20],
        "repository_areas": sorted({_area(p) for p in paths})[:25],
        "possible_entry_points": list(entries_by_area.values())[:18],
        "tree_complete": complete,
    }
    blocks = [json.dumps(summary, ensure_ascii=False)]
    for path in [name for name in _OVERVIEWS if name in paths][:2]:
        key = ("architecture_doc", pr.repo, pr.base_sha, path)
        if key not in shared:
            try:
                text = gh.get_file_text(pr.repo, path, pr.base_sha)
                shared[key] = text[:2500] if text and "\0" not in text else ""
            except (GitHubError, OSError):
                shared[key] = ""
        if shared[key]:
            blocks.append(json.dumps({"path": path, "text": shared[key]}, ensure_ascii=False))
    return "\n".join(blocks), candidate_paths
