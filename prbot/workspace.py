"""Local git checkout of a PR: fetch, run tools, diff, apply, commit, push.

Hardening, because the checkout may contain untrusted PR files:
  - credentials never appear in argv or logs; they reach git via GIT_CONFIG_* env vars,
    only for the one fetch/push that needs them
  - every git call disables hooks and fsmonitor and ignores the system/global config
  - tools run with an environment that has secrets removed. This is NOT a security
    boundary against code execution: PR code must only run in a job that holds no secrets
    (see `prbot analyze` and the README).
"""

from __future__ import annotations

import base64
import logging
import os
import re
import subprocess
from dataclasses import dataclass

from .models import PullRequest

log = logging.getLogger(__name__)

_SECRET_ENV = ("TOKEN", "SECRET", "KEY", "PASSWORD", "CREDENTIAL", "AUTH", "PRIVATE")
_GITHUB_FILES = ("GITHUB_ENV", "GITHUB_PATH", "GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY", "GITHUB_STATE")


def scrubbed_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not any(s in k.upper() for s in _SECRET_ENV) and k not in _GITHUB_FILES}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["UV_NO_CONFIG"] = "1"  # never read uv.toml / [tool.uv] from the PR checkout
    return env


def _git_env(token: str | None = None) -> dict[str, str]:
    env = scrubbed_env()
    for name in tuple(env):
        if name.startswith(("GIT_CONFIG_", "GIT_TRACE")) or name == "GIT_CURL_VERBOSE":
            env.pop(name)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_LITERAL_PATHSPECS"] = "1"  # a PR file named '*' must not match everything
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.extraheader"
        env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {basic}"
    return env


_GIT_SAFE = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "core.quotePath=false",
             "-c", "protocol.file.allow=never"]


@dataclass
class RunResult:
    code: int
    out: str


_REDACT = re.compile(r"(AUTHORIZATION: \w+ )\S+", re.I)


def run(cmd: list[str], cwd: str, timeout: int = 300, env: dict | None = None, stdin: str | None = None) -> RunResult:
    log.debug("run %s in %s", _REDACT.sub(r"\1***", " ".join(cmd)), cwd)
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout,
                           input=stdin.encode("utf-8", "surrogateescape") if stdin is not None else None,
                           env=env if env is not None else scrubbed_env())
    except FileNotFoundError:
        return RunResult(127, f"{cmd[0]}: not found")
    except subprocess.TimeoutExpired:
        return RunResult(124, f"{cmd[0]}: timed out after {timeout}s")
    # surrogateescape keeps CRs and non-UTF-8 bytes intact for diff/apply round trips
    out = (p.stdout or b"").decode("utf-8", "surrogateescape") + (p.stderr or b"").decode("utf-8", "replace")
    return RunResult(p.returncode, out)


def server_url() -> str:
    return os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")


class Workspace:
    """A clone of the base repository. PR heads are fetched via refs/pull/N/head."""

    def __init__(self, path: str, token: str | None = None):
        self.path = os.path.realpath(path)  # symlinks resolved, so containment checks work
        self.token = token

    def git(self, *args: str, token: str | None = None, timeout: int = 300) -> RunResult:
        return run(["git", *self._safe(), *args], self.path, timeout=timeout, env=_git_env(token))

    def _safe(self) -> list[str]:
        # the global config (where actions/checkout records safe.directory) is ignored
        return [*_GIT_SAFE, "-c", f"safe.directory={self.path}"]

    def git_out(self, *args: str) -> str:
        """stdout only (stderr dropped), for commands whose output is parsed."""
        try:
            p = subprocess.run(["git", *self._safe(), *args], cwd=self.path, capture_output=True, timeout=300,
                               env=_git_env())
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return (p.stdout or b"").decode("utf-8", "surrogateescape")

    def is_repo(self) -> bool:
        return self.git("rev-parse", "--is-inside-work-tree").code == 0

    def head(self) -> str:
        return self.git_out("rev-parse", "HEAD").strip()

    def current_ref(self) -> str:
        return self.git_out("symbolic-ref", "-q", "--short", "HEAD").strip() or self.head()

    def fetch_pr(self, pr: PullRequest) -> bool:
        """Fetch the PR head and base commits (no checkout, nothing executed)."""
        if not self.is_repo():
            log.warning("workspace %s is not a git repo", self.path)
            return False
        url = f"{server_url()}/{pr.repo}.git"
        r = self.git("fetch", "--no-tags", "--depth=50", url,
                     f"+refs/pull/{pr.number}/head:refs/prbot/pr-{pr.number}",
                     f"+refs/heads/{pr.base_ref}:refs/prbot/base-{pr.number}", token=self.token)
        if r.code != 0:
            log.warning("fetch failed: %s", r.out[-500:])
            return False
        return True

    def checkout(self, rev: str, *, detach: bool = True) -> bool:
        args = ["--detach"] if detach else []
        r = self.git("checkout", "--no-overwrite-ignore", *args, rev)
        if r.code != 0:
            log.warning("checkout of %s failed: %s", rev, r.out[-300:])
            return False
        return True

    def checkout_pr(self, pr: PullRequest) -> bool:
        return self.fetch_pr(pr) and self.checkout(pr.head_sha)

    def untracked(self) -> set[str]:
        out = self.git_out("ls-files", "--others", "--exclude-standard", "-z")
        return {p for p in out.split("\0") if p}

    def changed_paths(self) -> list[str]:
        out = self.git_out("diff", "--name-only", "-z", "HEAD")
        return [p for p in out.split("\0") if p]

    def diff(self, paths: list[str] | None = None) -> str:
        return self.git_out("diff", "-U0", "--no-color", "--no-ext-diff", "--no-textconv", "--", *(paths or []))

    def show(self, rev: str, path: str) -> str | None:
        r = subprocess.run(["git", *self._safe(), "show", f"{rev}:{path}"], cwd=self.path, capture_output=True,
                           env=_git_env())
        return r.stdout.decode("utf-8", "surrogateescape") if r.returncode == 0 else None

    def apply(self, patch: str, allowed: set[str] | None = None) -> bool:
        """Apply a zero-context patch, only if it touches nothing outside `allowed`."""
        base = ["git", *self._safe(), "apply", "--unidiff-zero", "--whitespace=nowarn"]
        if allowed is not None:
            r = run([*base, "--check", "-"], self.path, stdin=patch, env=_git_env())
            if r.code != 0:
                log.warning("git apply --check failed: %s", r.out[-500:])
                return False
            r = run([*base, "--numstat", "-z", "-"], self.path, stdin=patch, env=_git_env())
            touched = set()
            for entry in r.out.split("\0"):
                parts = entry.split("\t", 2)
                if len(parts) == 3 and parts[2]:
                    touched.add(parts[2].strip("\n"))
            if not touched or not touched <= allowed:
                log.warning("patch touches unexpected paths: %s", sorted(touched - (allowed or set())))
                return False
        r = run([*base, "-"], self.path, stdin=patch, env=_git_env())
        if r.code != 0:
            log.warning("git apply failed: %s", r.out[-500:])
        return r.code == 0

    def reset(self, keep_untracked: set[str] | None = None) -> None:
        """Restore tracked files and delete untracked files that tools or patches created."""
        self.git("reset", "--hard", "-q", "HEAD")
        if keep_untracked is None:
            return
        for rel in self.untracked() - keep_untracked:
            full = os.path.join(self.path, rel)
            parent = os.path.realpath(os.path.dirname(full))
            if os.path.islink(full) or (parent + os.sep).startswith(self.path + os.sep):
                try:
                    os.unlink(full)
                except OSError:
                    pass

    def has_credentials(self) -> bool:
        """True if the checkout's own git config holds a token (e.g. actions/checkout without
        persist-credentials: false)."""
        out = self.git_out("config", "--local", "--includes", "--get-regexp",
                           r"^(http\..*extraheader|credential\..*|url\..*)$")
        return bool(out.strip())

    def commit_and_push(self, pr: PullRequest, paths: list[str], message: str, push_token: str) -> bool:
        if not pr.head_repo or self.head() != pr.head_sha:
            return False
        r = self.git("add", "--", *paths)
        if r.code != 0:
            log.warning("could not stage fixes: %s", r.out[-500:])
            return False
        staged = set(self.git_out("diff", "--cached", "--name-only", "-z").rstrip("\0").split("\0"))
        if not staged <= set(paths):
            log.warning("refusing to commit unexpected staged changes")
            return False
        r = self.git("-c", "user.name=prbot", "-c", "user.email=prbot@users.noreply.github.com",
                     "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", message)
        if r.code != 0:
            log.warning("commit failed: %s", r.out[-500:])
            return False
        url = f"{server_url()}/{pr.head_repo}.git"
        ref = f"refs/heads/{pr.head_ref}"
        commit = self.head()
        r = self.git("push", "--no-verify", f"--force-with-lease={ref}:{pr.head_sha}", url,
                     f"{commit}:{ref}", token=push_token)
        if r.code != 0:
            log.warning("push failed: %s", r.out[-500:])
            remote = self.git("ls-remote", "--refs", url, ref, token=push_token)
            if remote.code == 0 and remote.out.split("\t", 1)[0] == commit:
                return True
            return False
        return True
