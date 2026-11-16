"""Configuration: built-in defaults deep-merged with the repo's .github/prereview.yml.

The repo config is always read from the *base* branch, never from the PR head,
so a PR cannot change how it is reviewed.
"""

from __future__ import annotations

import copy
from typing import Any

import yaml

CONFIG_PATHS = (".github/prereview.yml", ".github/prereview.yaml", ".prereview.yml")

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "skip_drafts": True,
    "skip_authors": ["dependabot[bot]", "renovate[bot]"],
    # Paths ignored by every check (generated code, lockfiles, vendored deps).
    "ignore_paths": [
        "**/*.lock",
        "**/package-lock.json",
        "**/pnpm-lock.yaml",
        "**/yarn.lock",
        "**/poetry.lock",
        "**/uv.lock",
        "**/Cargo.lock",
        "**/go.sum",
        "vendor/**",
        "**/node_modules/**",
        "**/*.min.js",
        "**/dist/**",
    ],
    "checks": {
        "ci": {
            "enabled": True,
            "severity": "blocking",
            "required": [],  # exact check-run names or commit-status contexts that must report
            "ignore": ["pre-review"],
            # Gate jobs that only fail because another job failed. Hidden when a real failure
            # is listed, so authors see the job that actually broke.
            "aggregate_patterns": [
                r"(?i)\ball\b.*\b(checks?|jobs?|tests?)\b.*\b(pass|passed|green|succeed|ok)\b",
                r"(?i)\brequired[- _]checks?\b",
                r"(?i)^(ci|checks?)[- _]?(ok|success|passed|gate|status)$",
            ],
            "show_logs": True,  # set false if the repo already posts CI logs in a comment
        },
        "mergeable": {"enabled": True, "severity": "blocking", "behind_severity": "advisory"},
        "size": {"enabled": True, "severity": "advisory", "max_lines": 800, "max_files": 40},
        "tests": {
            "enabled": True,
            "severity": "advisory",
            # Code changes in these paths require test changes. Empty = everywhere.
            "required_paths": [],
            "test_patterns": [
                "**/test_*.py",
                "**/*_test.py",
                "**/tests/**",
                "**/test/**",
                "**/__tests__/**",
                "**/*.test.*",
                "**/*.spec.*",
                "**/*_test.go",
                "**/*Test.java",
                "**/*_spec.rb",
            ],
            "code_extensions": [
                ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt",
                ".rb", ".php", ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".swift", ".scala",
            ],
            "min_code_lines": 10,  # tiny code changes do not need tests
            # Make missing tests blocking when the PR title matches, e.g. ["^fix"] for bug fixes.
            "blocking_title_patterns": [],
        },
        "description": {
            "enabled": True,
            "severity": "advisory",
            "min_length": 30,
            "require_linked_issue": False,
        },
        "debris": {"enabled": True, "severity": "blocking", "max_file_kb": 1024},
        "overlap": {
            "enabled": True,
            "severity": "advisory",
            "max_prs_scanned": 100,
            "max_files_per_pr": 100,  # higher values fetch further pages for large PRs
            "min_file_overlap": 0.5,  # Jaccard similarity of the two PRs' (non-hub) file sets
            "title_similarity": 0.6,  # with at least one shared file
            "title_only_similarity": 0.85,  # flagged even without shared files
            # A file touched by more open PRs than this is a hub (e.g. a central module);
            # sharing it says nothing about duplication.
            "hub_file_prs": 4,
        },
        "lint": {
            "enabled": True,
            "severity": "blocking",
            # Run linters on fork PRs only if every detected tool is safe for untrusted code.
            "tools": "auto",  # "auto" or an explicit list of tool names
            # Only run tools the repo itself enforces (in CI, hooks, justfile, Makefile...).
            "require_enforcement": True,
        },
        "llm_review": {
            "enabled": True,
            "severity": "advisory",  # set to "blocking" once the team trusts it
            "only_when_clean": True,  # skip PRs that already have blocking findings
            "min_confidence": 0.75,
            "max_findings": 5,
            "max_diff_chars": 60000,
            "context_paths": [],  # additional exact repository paths to inspect for related code
        },
    },
    # When to run repo tools (linters, formatters) against PR code.
    #   never      - only use GitHub API data
    #   same-repo  - run tools for branches in the base repo; forks only with safe tools
    #   always     - run every detected tool on every PR (only in an isolated runner!)
    "execution": "same-repo",
    "autofix": {
        "enabled": True,
        # commit  - push fix commits to the PR branch when permitted, else fall back to suggest
        # suggest - post GitHub suggestion blocks the author can apply in one click
        # comment - report findings with instructions for the author
        "mode": "commit",
        "update_branch": True,  # merge base into a PR that is behind but has no conflicts
        "commit_message": "prbot: apply automatic fixes",
    },
    "llm": {
        # hermes | openai-compatible | anthropic | none.
        # "auto": hermes when ORBIT_LLM_BASE_URL is set, else anthropic when ANTHROPIC_API_KEY is set.
        "provider": "auto",
        # Hermes defaults to the Hugging Face repo IDs used by vLLM and SGLang. Other providers
        # require a review model ID and use it for triage when no small model is set.
        "small_model": None,
        "review_model": None,
        "effort": "medium",  # anthropic only
        "base_url": None,  # OpenAI-compatible endpoint; or set ORBIT_LLM_BASE_URL
        "temperature": 0.6,  # Hermes 4 model card recommends 0.6 / top_p 0.95 / top_k 20
        "top_p": 0.95,
        "json_mode": True,  # send response_format=json_object; auto-disabled if the server rejects it
        "max_reviews_per_run": 20,  # hard budget on deep reviews per invocation
        "use_fallbacks": True,  # server-side refusal fallback for Anthropic
    },
    "comments": {
        "max_inline": 5,
        "stale_days": 21,
        "tag_author": False,
    },
    "analysis": {
        # Download lint results from the separate, secret-free analysis job (see README).
        "artifact": True,
        # Path of that workflow; artifacts from any other workflow are ignored.
        "workflow": ".github/workflows/prereview-analyze.yml",
        # Keep the status pending (not "ready") until the analysis for the head commit arrives.
        # Set false if you do not run the analysis workflow.
        "required": True,
    },
    "labels": {
        "needs_changes": "bot:needs-changes",
        "ready": "bot:ready-for-review",
        "disputed": "bot:disputed",
        "duplicate": "bot:possible-duplicate",
        "stale": "bot:stale",
        "paused": "bot:paused",
    },
    "status_context": "pre-review",
}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


class Config(dict):
    """A dict with helpers for nested lookups."""

    def check(self, name: str) -> dict[str, Any]:
        return self["checks"].get(name, {"enabled": False})

    def check_enabled(self, name: str) -> bool:
        return bool(self.check(name).get("enabled"))

    def label(self, name: str) -> str:
        return self["labels"][name]


def autofix_mode(cfg: dict[str, Any]) -> str:
    options = cfg.get("autofix", {})
    mode = options.get("mode")
    if options.get("enabled") and mode in ("commit", "suggest"):
        return mode
    return "comment"


def _normalize(user: dict[str, Any]) -> dict[str, Any]:
    """`checks: {lint: false}` means disabled, `size: null` means defaults; sections stay dicts."""
    out = dict(user)
    checks = out.get("checks")
    if checks is None:
        out.pop("checks", None)
    elif isinstance(checks, dict):
        fixed = {}
        for name, value in checks.items():
            if isinstance(value, bool):
                fixed[name] = {"enabled": value}
            elif value is None:
                continue
            elif isinstance(value, dict):
                fixed[name] = value
            else:
                raise ValueError(f"checks.{name} must be true, false or a mapping")
        out["checks"] = fixed
    else:
        raise ValueError("checks must be a mapping")
    for section, default in DEFAULTS.items():
        if isinstance(default, dict) and section in out and section != "checks":
            if out[section] is None:
                out.pop(section)
            elif not isinstance(out[section], dict):
                raise ValueError(f"{section} must be a mapping")
    return out


def parse_config_dict(user: dict[str, Any]) -> Config:
    cfg = Config(deep_merge(DEFAULTS, _normalize(user or {})))
    ci = cfg.check("ci")
    for field in ("required", "ignore"):
        names = ci[field]
        if not isinstance(names, list) or any(not isinstance(n, str) or not n.strip() for n in names):
            raise ValueError(f"checks.ci.{field} must be a list of nonempty check names")
    context = cfg["status_context"]
    if not isinstance(context, str) or not context.strip():
        raise ValueError("status_context must be a nonempty string")
    if set(ci["required"]) & {*ci["ignore"], context}:
        raise ValueError("checks.ci.required cannot include ignored checks or Orbit's own status")
    return cfg


def parse_config(text: str | None) -> Config:
    user: dict[str, Any] = {}
    if text:
        try:
            loaded = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValueError("Invalid YAML in prereview config") from exc
        if loaded is not None and not isinstance(loaded, dict):
            raise ValueError("prereview config must be a mapping at the top level")
        user = loaded or {}
    return parse_config_dict(user)


def load_config(gh, repo: str, ref: str) -> Config:
    """Fetch the repo's config from `ref` (the base branch) and merge with defaults."""
    for path in CONFIG_PATHS:
        text = gh.get_file_text(repo, path, ref)
        if text is not None:
            return parse_config(text)
    return parse_config(None)
