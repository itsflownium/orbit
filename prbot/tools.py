"""Auto-detected linters and formatters, matched to what the repo really enforces.

Nothing is hardcoded per repo. For each known tool the bot asks three questions of the checkout:

All three questions are answered from the BASE branch checkout, before the PR head is
checked out, so a PR cannot choose which tools run, which versions, or whether they count.

1. Is it configured?   (ruff.toml, [tool.black], go.mod, .prettierrc, eslint config...)
2. Is it enforced?     (named in CI workflows, pre-commit/lefthook hooks, justfile, Makefile,
                        package.json scripts, tox/nox). Configured-but-unenforced tools are skipped,
                        so the bot never blocks a PR on a rule the repo does not apply itself.
3. Which version?      (pins in pyproject.toml, requirements*.txt, .pre-commit-config.yaml). If the
                        installed version differs, the pinned one runs through `uvx`; without uv the
                        tool's findings are downgraded to advisory.

``safe_untrusted`` marks tools that do not execute code from the files they check, so they may
run on fork PRs. ESLint/Prettier configs can be JavaScript, and `cargo fmt` goes through rustup,
which honours a PR-supplied rust-toolchain.toml, so those are not safe.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable

Issue = tuple[str, int | None, str]  # path, line, message

LINT = "lint"  # reports line-level problems; has a check command
FORMAT = "format"  # rewrites files; problems are found by diffing its output


def _read(ws: str, name: str) -> str:
    try:
        with open(os.path.join(ws, name), encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _exists(ws: str, *names: str) -> bool:
    return any(os.path.exists(os.path.join(ws, n)) for n in names)


def _package_json(ws: str) -> dict:
    try:
        return json.loads(_read(ws, "package.json") or "{}")
    except ValueError:
        return {}


def _has_dep(ws: str, dep: str) -> bool:
    pkg = _package_json(ws)
    return any(dep in (pkg.get(k) or {}) for k in ("dependencies", "devDependencies"))


# ---------------------------------------------------------------- enforcement evidence

_EVIDENCE_GLOBS = (
    ".github/workflows/*.yml", ".github/workflows/*.yaml", ".github/actions/*/action.yml",
    ".pre-commit-config.yaml", ".pre-commit-config.yml", "lefthook.yml", "lefthook.yaml", ".lefthook.yml",
    "justfile", "Justfile", ".justfile", "Makefile", "makefile", "GNUmakefile", "package.json",
    "tox.ini", "noxfile.py", "Taskfile.yml", "Taskfile.yaml", ".gitlab-ci.yml", ".circleci/config.yml",
    ".husky/*", ".lintstagedrc", ".lintstagedrc.json", "lint-staged.config.js", "setup.cfg",
)


def _evidence(ws: str) -> str:
    chunks = []
    for pattern in _EVIDENCE_GLOBS:
        for path in sorted(glob.glob(os.path.join(ws, pattern))):
            if not os.path.isfile(path):
                continue
            rel = os.path.relpath(path, ws)
            if os.path.basename(rel) == "package.json":
                # only scripts count as enforcement; a devDependency is not
                scripts = (_package_json(ws).get("scripts") or {}) if rel == "package.json" else {}
                chunks.append("\n".join(f"{k}: {v}" for k, v in scripts.items()))
            else:
                chunks.append(_read(ws, rel))
    return "\n".join(chunks)


# ---------------------------------------------------------------- version pins

def _pin_from_text(text: str, pkg: str) -> str | None:
    name = re.escape(pkg)
    # ruff==0.4.1, black[jupyter]==24.1.0
    m = re.search(rf"(?<![\w.-]){name}(?:\[[^\]]*\])?\s*===?\s*v?([0-9][\w.+-]*)", text)
    if m:
        return m.group(1)
    # Poetry: ruff = "0.4.1" or ruff = "==0.4.1" (exact versions only)
    m = re.search(rf"(?m)^\s*{name}\s*=\s*[\"'](?:==)?([0-9][\w.+-]*)[\"']", text)
    return m.group(1) if m else None


def pinned_version(ws: str, pkg: str) -> str | None:
    """Exact version the repo pins for a Python tool, if any."""
    sources = ["pyproject.toml", "requirements-dev.txt", "requirements_dev.txt", "dev-requirements.txt",
               "requirements.txt", "requirements/dev.txt", "requirements/lint.txt", "setup.cfg", "tox.ini"]
    sources += [os.path.relpath(p, ws) for p in sorted(glob.glob(os.path.join(ws, "requirements*/*.txt")))]
    for name in sources:
        v = _pin_from_text(_read(ws, name), pkg)
        if v:
            return v
    pre = _read(ws, ".pre-commit-config.yaml")
    repo_name = {"ruff": "ruff-pre-commit", "black": "psf/black"}.get(pkg, pkg)
    m = re.search(rf"repo:\s*\S*{re.escape(repo_name)}\S*\s*\n\s*rev:\s*['\"]?v?([0-9][\w.+-]*)", pre)
    return m.group(1) if m else None


@lru_cache(maxsize=32)
def installed_version(binary: str) -> str | None:
    if not shutil.which(binary):
        return None
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"(\d+\.\d+(?:\.\d+)?)", out)
    return m.group(1) if m else None


# ---------------------------------------------------------------- output parsing

_LOC = re.compile(r"^(?P<path>[^\s:][^:]*):(?P<line>\d+)(?::\d+)?:?\s*(?P<msg>.*)$")


def parse_locations(output: str, files: list[str], ws: str | None = None) -> list[Issue]:
    """Generic `path:line[:col]: message` parser, keeping only files in the PR."""
    wanted = set(files)
    issues: list[Issue] = []
    for raw in output.splitlines():
        m = _LOC.match(raw.strip())
        if not m:
            continue
        path = m.group("path")
        if path.startswith("./"):
            path = path[2:]
        elif ws and os.path.isabs(path):
            path = os.path.relpath(path, ws)
        if path in wanted:
            issues.append((path, int(m.group("line")), m.group("msg").strip()))
    return issues


# ---------------------------------------------------------------- tool table

@dataclass
class Tool:
    name: str
    kind: str  # LINT or FORMAT
    exts: tuple[str, ...]
    detect: Callable[[str], bool]
    enforce: str  # regex searched in CI/hook files
    argv: list[str]  # base command
    fix_args: Callable[[list[str]], list[list[str]]]
    check_args: Callable[[list[str]], list[str]] | None = None
    pip: str | None = None  # Python package name, for version pins
    safe_untrusted: bool = True
    # resolved per workspace by resolve()
    command: list[str] = field(default_factory=list)
    version_note: str | None = None
    exact_version: bool = True

    def available(self) -> bool:
        return shutil.which(self.argv[0]) is not None

    def is_enforced(self, ws: str) -> bool:
        return re.search(self.enforce, _evidence(ws), re.M) is not None

    def parse(self, output: str, files: list[str], ws: str | None = None) -> list[Issue]:
        if self.name == "eslint":
            return _eslint_issues(output, files, ws)
        return parse_locations(output, files, ws)

    def check_cmd(self, files: list[str]) -> list[str]:
        assert self.check_args is not None
        return [*self.command, *self.check_args(files)]

    def fix_cmds(self, files: list[str]) -> list[list[str]]:
        return [[*self.command, *args] for args in self.fix_args(files)]

    def resolve(self, ws: str) -> "Tool | None":
        """Pick the binary to run (pinned via uvx when needed). None if it cannot run.
        `ws` must be the base-branch checkout."""
        t = Tool(**{k: getattr(self, k) for k in ("name", "kind", "exts", "detect", "enforce", "argv",
                                                   "fix_args", "check_args", "pip", "safe_untrusted")})
        t.command = list(self.argv)
        pin = pinned_version(ws, self.pip) if self.pip else None
        have = installed_version(self.argv[0]) if self.pip else None
        if pin and have != pin:
            if shutil.which("uvx"):
                t.command = ["uvx", "--isolated", f"{self.pip}@{pin}", *self.argv[1:]]
                t.version_note = f"`{self.name}` {pin} (the repo's pinned version)"
                return t
            if not have:
                return None
            t.exact_version = False
            t.version_note = f"`{self.name}` {have} (the repo pins {pin}; install uv to run the exact version)"
            return t
        if not t.available():
            if self.pip and shutil.which("uvx"):
                t.command = ["uvx", "--isolated", self.pip, *self.argv[1:]]
                t.version_note = f"`{self.name}` (latest, via uvx)"
                return t
            return None
        if self.pip and have:
            t.version_note = f"`{self.name}` {have}"
        return t


def _ruff_detect(ws: str) -> bool:
    return _exists(ws, "ruff.toml", ".ruff.toml") or "[tool.ruff" in _read(ws, "pyproject.toml") \
        or "ruff-pre-commit" in _read(ws, ".pre-commit-config.yaml")


def _black_detect(ws: str) -> bool:
    return "[tool.black]" in _read(ws, "pyproject.toml") or "psf/black" in _read(ws, ".pre-commit-config.yaml")


_PRETTIER_FILES = (".prettierrc", ".prettierrc.json", ".prettierrc.yml", ".prettierrc.yaml", ".prettierrc.js",
                   ".prettierrc.cjs", ".prettierrc.mjs", "prettier.config.js", "prettier.config.mjs", "prettier.config.cjs")
_ESLINT_FILES = ("eslint.config.js", "eslint.config.mjs", "eslint.config.cjs", "eslint.config.ts",
                 ".eslintrc", ".eslintrc.js", ".eslintrc.cjs", ".eslintrc.json", ".eslintrc.yml", ".eslintrc.yaml")


def _eslint_issues(output: str, files: list[str], ws: str | None) -> list[Issue]:
    try:
        rows, _ = json.JSONDecoder().raw_decode(output.lstrip())
    except ValueError:
        return []
    if not isinstance(rows, list):
        return []
    wanted = set(files)
    issues: list[Issue] = []
    for row in rows:
        path = row["filePath"]
        if os.path.isabs(path) and ws:
            path = os.path.relpath(path, ws)
        if path not in wanted:
            continue
        for msg in row["messages"]:
            if msg.get("severity") != 2:
                continue
            detail = msg["message"]
            if msg.get("ruleId"):
                detail = f"{msg['ruleId']} {detail}"
            issues.append((path, msg.get("line"), detail))
    return issues


TOOLS: list[Tool] = [
    Tool("ruff", LINT, (".py", ".pyi"), _ruff_detect,
         enforce=r"ruff[-\s'\"]+check|ruff_runner|^\s*-?\s*id:\s*['\"]?ruff['\"]?\s*(?:#.*)?$|\bruff\s+\.|just\s+ruff|ruff-action",
         argv=["ruff"], pip="ruff",
         check_args=lambda fs: ["check", "--output-format=concise", "--no-fix", "--quiet", "--", *fs],
         fix_args=lambda fs: [["check", "--fix", "--quiet", "--", *fs]]),
    Tool("ruff-format", FORMAT, (".py", ".pyi"), _ruff_detect,
         enforce=r"ruff[\s'\"]+format|id:\s*ruff-format",
         argv=["ruff"], pip="ruff",
         fix_args=lambda fs: [["format", "--quiet", "--", *fs]]),
    Tool("black", FORMAT, (".py", ".pyi"), _black_detect,
         enforce=r"(?<![\w-])black(?![\w-])",
         argv=["black"], pip="black",
         fix_args=lambda fs: [["--quiet", "--", *fs]]),
    Tool("gofmt", FORMAT, (".go",), lambda ws: _exists(ws, "go.mod"),
         enforce=r"gofmt|gofumpt|golangci-lint|goimports",
         argv=["gofmt"], fix_args=lambda fs: [["-w", "--", *fs]]),
    Tool("rustfmt", FORMAT, (".rs",), lambda ws: _exists(ws, "Cargo.toml"),
         enforce=r"cargo\s+fmt|rustfmt",
         argv=["cargo", "fmt"], fix_args=lambda fs: [["--", *fs]], safe_untrusted=False),
    Tool("prettier", FORMAT,
         (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".css", ".scss", ".json", ".md", ".yml", ".yaml", ".html", ".vue"),
         lambda ws: _has_dep(ws, "prettier") or _exists(ws, *_PRETTIER_FILES),
         enforce=r"prettier\b[^\n]*(?:--check|\s-c\b|--list-different|\s-l\b)|format:check|lint-staged",
         argv=["npx", "--no-install", "prettier"],
         fix_args=lambda fs: [["--write", "--log-level", "warn", *fs]], safe_untrusted=False),
    Tool("eslint", LINT, (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"),
         lambda ws: _has_dep(ws, "eslint") and _exists(ws, *_ESLINT_FILES),
         enforce=r"eslint",
         argv=["npx", "--no-install", "eslint"],
         check_args=lambda fs: ["--format", "json", *fs],
         fix_args=lambda fs: [["--fix", *fs]], safe_untrusted=False),
]


def detect_tools(ws: str, wanted: str | list[str] = "auto", require_enforcement: bool = True,
                 *, unavailable: list[Tool] | None = None) -> list[Tool]:
    """Tools to run in this checkout, already resolved to a runnable command."""
    if wanted != "auto":
        names = set(wanted)
        candidates = [t for t in TOOLS if t.name in names]
    else:
        candidates = [t for t in TOOLS if t.detect(ws) and (not require_enforcement or t.is_enforced(ws))]
    resolved = []
    for tool in candidates:
        ready = tool.resolve(ws)
        if ready is not None:
            resolved.append(ready)
        elif unavailable is not None:
            unavailable.append(tool)
    # Two formatters for the same files would fight; prefer the enforced/first one.
    if any(t.name == "ruff-format" for t in resolved):
        resolved = [t for t in resolved if t.name != "black"]
    return resolved


def python_pins(ws: str) -> list[str]:
    """pip requirements for the repo's pinned Python tools (used by the GitHub Action)."""
    out = []
    for pkg in ("ruff", "black"):
        v = pinned_version(ws, pkg)
        if v:
            out.append(f"{pkg}=={v}")
    return out
