"""Minimal GitHub REST client built on the standard library."""

from __future__ import annotations

import base64
import email.utils
import http.client
import io
import json
import logging
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from typing import Any, Iterator

from . import orbit_env
from .models import ChangedFile, PullRequest

log = logging.getLogger(__name__)


def ssl_context() -> ssl.SSLContext:
    """Use certifi's CA bundle when available (python.org macOS builds ship without one)."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


SSL_CONTEXT = ssl_context()


class _StripAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """urllib copies every header to the redirect target. GitHub log and artifact downloads
    redirect to blob storage on another host; the token must not follow."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        current = urllib.parse.urlsplit(req.full_url)
        target = urllib.parse.urlsplit(newurl)
        if new is not None and (current.scheme, current.netloc) != (target.scheme, target.netloc):
            for name in list(new.headers):
                if name.lower() == "authorization":
                    del new.headers[name]
            new.unredirected_hdrs.pop("Authorization", None)
        return new


_OPENER = urllib.request.build_opener(urllib.request.HTTPSHandler(context=SSL_CONTEXT), _StripAuthOnRedirect())

_IDEMPOTENT = {"GET", "HEAD", "PUT", "DELETE"}


def _retry_after(value: str | None, default: int) -> int:
    if not value:
        return default
    try:
        return max(0, min(int(value), 300))
    except ValueError:
        try:  # HTTP-date form
            when = email.utils.parsedate_to_datetime(value).timestamp()
            return max(0, min(int(when - time.time()) + 1, 300))
        except (TypeError, ValueError):
            return default


def graphql_url(api: str) -> str:
    explicit = os.environ.get("GITHUB_GRAPHQL_URL")
    if explicit:
        return explicit
    if api.endswith("/api/v3"):  # GitHub Enterprise Server
        return api[: -len("/v3")] + "/graphql"
    return f"{api}/graphql"


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str, url: str):
        super().__init__(f"GitHub API {status} on {url}: {message}")
        self.status = status


def cached_tree(gh, repo: str, ref: str, shared: dict) -> tuple[dict[str, tuple[str, str]], bool]:
    key = ("git_tree", repo, ref)
    if key not in shared:
        try:
            shared[key] = gh.get_tree(repo, ref)
        except (GitHubError, OSError) as exc:
            log.info("Could not read optional repository tree: %s", type(exc).__name__)
            shared[key] = ({}, False)
    return shared[key]


class GitHub:
    def __init__(self, token: str | None = None, api_url: str | None = None, dry_run: bool = False,
                 *, checks_token: str | None = None, using_git_credentials: bool = False):
        self.token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        self.api = (api_url or os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
        self.dry_run = dry_run
        self.using_git_credentials = using_git_credentials
        self.writes: list[tuple[str, str, Any]] = []  # recorded writes in dry-run mode
        self._login: str | None = None
        self._checks = GitHub(token=checks_token, api_url=self.api, dry_run=dry_run) if checks_token else None

    # ------------------------------------------------------------------ transport
    def graphql(self, query: str, variables: dict) -> dict:
        """GraphQL queries are reads, so they run even in dry-run mode."""
        data = self.request("POST", graphql_url(self.api), {"query": query, "variables": variables}, read=True)
        if data.get("errors"):
            raise GitHubError(200, "; ".join(e.get("message", "") for e in data["errors"]), "/graphql")
        return data

    def request(self, method: str, path: str, body: Any = None, params: dict | None = None,
                accept: str = "application/vnd.github+json", raw: bool = False, read: bool = False,
                binary: bool = False) -> Any:
        if method != "GET" and self.dry_run and not read:
            self.writes.append((method, path, body))
            log.info("dry-run: %s %s", method, path)
            return {}
        url = path if path.startswith("http") else f"{self.api}{path}"
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "prbot",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if data is not None:
            headers["Content-Type"] = "application/json"
        for attempt in range(4):
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with _OPENER.open(req, timeout=60) as resp:
                    payload = resp.read()
                    self._last_link = resp.headers.get("Link", "")
                    if binary:
                        return payload
                    if raw:
                        return payload.decode("utf-8", "replace")
                    return json.loads(payload) if payload else {}
            except urllib.error.HTTPError as e:
                text = e.read().decode("utf-8", "replace")
                # A 5xx on a POST/PATCH may have been applied already; retrying could post twice.
                server_error = e.code in (502, 503, 504) and (method in _IDEMPOTENT or read)
                rate_limited = e.code in (403, 429) and ("rate limit" in text.lower() or e.headers.get("Retry-After"))
                if (server_error or rate_limited) and attempt < 3:
                    wait = _retry_after(e.headers.get("Retry-After"), 2 ** (attempt + 1))
                    reset = e.headers.get("X-RateLimit-Reset")
                    if reset and e.headers.get("X-RateLimit-Remaining") == "0":
                        wait = max(wait, min(int(reset) - int(time.time()) + 1, 300))
                    log.warning("GitHub %s on %s, retrying in %ss", e.code, path, wait)
                    time.sleep(wait)
                    continue
                try:
                    message = json.loads(text).get("message", text)
                except ValueError:
                    message = text
                raise GitHubError(e.code, message, url) from None
            except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
                # urllib wraps only connect errors in URLError; a dropped connection while reading
                # the response surfaces as RemoteDisconnected/ConnectionResetError/TimeoutError
                reason = getattr(e, "reason", e)
                if attempt < 3 and (method in _IDEMPOTENT or read) \
                        and not isinstance(reason, ssl.SSLCertVerificationError):
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise
        raise RuntimeError("unreachable")

    def paginate(self, path: str, params: dict | None = None, limit: int | None = None) -> Iterator[Any]:
        params = dict(params or {})
        params.setdefault("per_page", 100)
        url: str | None = path
        count = 0
        first = True
        while url:
            page = self.request("GET", url, params=params if first else None)
            first = False
            items = page if isinstance(page, list) else _envelope_items(page)
            for item in items:
                yield item
                count += 1
                if limit and count >= limit:
                    return
            url = _next_link(getattr(self, "_last_link", ""))

    # ------------------------------------------------------------------ reads
    def login(self) -> str:
        if self._login is None:
            self._login = orbit_env("LOGIN")
        if self._login is None:
            try:
                self._login = self.request("GET", "/user").get("login", "")
            except GitHubError:
                # GitHub App installation tokens and GITHUB_TOKEN cannot call /user
                try:
                    self._login = self.graphql("query { viewer { login } }", {})["data"]["viewer"]["login"]
                except (GitHubError, KeyError, TypeError):
                    self._login = ""
            if not self._login:
                self._login = "github-actions[bot]"
                log.warning("could not determine the bot's login; assuming %s. Set ORBIT_LOGIN (action input "
                            "`bot-login`) when posting with a GitHub App token.", self._login)
            elif self._login.lower() == "github-actions" or self._login.lower() == "github-actions[bot]":
                self._login = "github-actions[bot]"
        return self._login

    def get_pr(self, repo: str, number: int, with_files: bool = True) -> PullRequest:
        data = self.request("GET", f"/repos/{repo}/pulls/{number}")
        if data.get("mergeable") is None and data.get("state") == "open":
            # GitHub computes mergeability lazily; ask again after a moment.
            time.sleep(2)
            data = self.request("GET", f"/repos/{repo}/pulls/{number}")
        pr = PullRequest.from_api(data)
        if with_files:
            pr.files = self.get_pr_files(repo, number)
        return pr

    def get_pr_files(self, repo: str, number: int) -> list[ChangedFile]:
        return [
            ChangedFile(
                filename=f["filename"],
                status=f.get("status", "modified"),
                additions=f.get("additions", 0),
                deletions=f.get("deletions", 0),
                patch=f.get("patch"),
                previous_filename=f.get("previous_filename"),
            )
            for f in self.paginate(f"/repos/{repo}/pulls/{number}/files", limit=3000)
        ]

    def list_open_prs(self, repo: str, limit: int | None = None, *, head: str | None = None) -> list[dict]:
        params = {"state": "open", "sort": "updated", "direction": "desc"}
        if head:
            params["head"] = head
        return list(self.paginate(f"/repos/{repo}/pulls", params, limit=limit))

    def get_pr_diff(self, repo: str, number: int) -> str:
        return self.request("GET", f"/repos/{repo}/pulls/{number}", accept="application/vnd.github.diff", raw=True)

    def get_file_text(self, repo: str, path: str, ref: str) -> str | None:
        try:
            data = self.request("GET", f"/repos/{repo}/contents/{urllib.parse.quote(path)}", params={"ref": ref})
        except GitHubError as e:
            if e.status == 404:
                return None
            raise
        if isinstance(data, dict) and data.get("encoding") == "base64":
            return base64.b64decode(data["content"]).decode("utf-8", "replace")
        return None

    def get_tree(self, repo: str, ref: str) -> tuple[dict[str, tuple[str, str]], bool]:
        """Return paths and object identities, plus whether GitHub returned the entire tree."""
        data = self.request("GET", f"/repos/{repo}/git/trees/{urllib.parse.quote(ref, safe='')}",
                            params={"recursive": "1"})
        entries = {entry["path"]: (entry["mode"], entry["sha"]) for entry in data["tree"]
                   if entry["type"] in ("blob", "commit")}
        return entries, not data.get("truncated", True)

    def compare_status(self, repo: str, base_sha: str, head_sha: str) -> str | None:
        data = self.request("GET", f"/repos/{repo}/compare/{base_sha}...{head_sha}", params={"per_page": 1})
        return data.get("status")

    def check_runs(self, repo: str, sha: str) -> list[dict]:
        # GitHub does not let a fine-grained personal token opt into Checks access.
        client = self._checks or self
        return list(client.paginate(f"/repos/{repo}/commits/{sha}/check-runs"))

    def combined_status(self, repo: str, sha: str) -> dict:
        data = self.request("GET", f"/repos/{repo}/commits/{sha}/status", params={"per_page": 100})
        statuses = list(data.get("statuses", []))
        while next_page := _next_link(getattr(self, "_last_link", "")):
            statuses.extend(self.request("GET", next_page).get("statuses", []))
        return {**data, "statuses": statuses}

    def get_labels(self, repo: str, number: int) -> set[str]:
        return {lbl["name"] for lbl in self.paginate(f"/repos/{repo}/issues/{number}/labels")}

    def issue_comments(self, repo: str, number: int) -> list[dict]:
        return list(self.paginate(f"/repos/{repo}/issues/{number}/comments"))

    def job_log_tail(self, repo: str, job_id: int, lines: int = 40) -> str | None:
        """Last lines of an Actions job log, or None if unavailable."""
        try:
            text = self.request("GET", f"/repos/{repo}/actions/jobs/{job_id}/logs", raw=True, accept="*/*")
        except (GitHubError, urllib.error.URLError, http.client.HTTPException, OSError):
            return None
        return "\n".join(text.splitlines()[-lines:])

    # ------------------------------------------------------------------ writes
    def create_comment(self, repo: str, number: int, body: str) -> dict:
        return self.request("POST", f"/repos/{repo}/issues/{number}/comments", {"body": body})

    def update_comment(self, repo: str, comment_id: int, body: str) -> dict:
        return self.request("PATCH", f"/repos/{repo}/issues/comments/{comment_id}", {"body": body})

    def create_review(self, repo: str, number: int, sha: str, comments: list[dict], body: str = "") -> dict:
        return self.request("POST", f"/repos/{repo}/pulls/{number}/reviews",
                            {"commit_id": sha, "event": "COMMENT", "body": body, "comments": comments})

    def ensure_label(self, repo: str, name: str, color: str, description: str) -> None:
        try:
            self.request("POST", f"/repos/{repo}/labels",
                         {"name": name, "color": color, "description": description})
        except GitHubError as e:
            if e.status != 422:  # 422 = already exists
                raise

    def add_labels(self, repo: str, number: int, labels: list[str]) -> None:
        if labels:
            self.request("POST", f"/repos/{repo}/issues/{number}/labels", {"labels": labels})

    def remove_label(self, repo: str, number: int, label: str) -> None:
        try:
            self.request("DELETE", f"/repos/{repo}/issues/{number}/labels/{urllib.parse.quote(label, safe='')}")
        except GitHubError as e:
            if e.status != 404:
                raise

    def set_status(self, repo: str, sha: str, state: str, context: str, description: str, url: str | None = None) -> None:
        body = {"state": state, "context": context, "description": description[:140]}
        if url:
            body["target_url"] = url
        self.request("POST", f"/repos/{repo}/statuses/{sha}", body)

    def update_branch(self, repo: str, number: int, expected_sha: str) -> bool:
        try:
            self.request("PUT", f"/repos/{repo}/pulls/{number}/update-branch", {"expected_head_sha": expected_sha})
            return True
        except GitHubError as e:
            log.info("update-branch failed: %s", e)
            return False

    def permission(self, repo: str, user: str) -> str:
        """The user's effective permission on the repo: admin, maintain, write, triage, read or none."""
        try:
            data = self.request("GET", f"/repos/{repo}/collaborators/{urllib.parse.quote(user, safe='')}/permission")
        except GitHubError as e:
            if e.status in (403, 404):
                return "none"
            raise
        # custom repository roles have their own role_name but inherit a base permission
        legacy = data.get("permission") or "none"  # admin, write, read or none
        role = data.get("role_name") or legacy
        if role in ("admin", "maintain", "write"):
            return role
        return "write" if legacy in ("admin", "write") else role

    def analysis_artifacts(self, repo: str, name: str, pr: PullRequest, workflow_path: str | None) -> list[dict]:
        """Candidate artifacts for this PR's head commit, newest first.

        Only artifacts from a `pull_request` run of the analysis workflow, for this PR's head
        repository and branch, are considered. Every candidate is returned (not just the
        newest) so a stray artifact for the same commit cannot hide the real one.
        """
        out = []
        runs: dict[int, dict] = {}
        for art in self.paginate(f"/repos/{repo}/actions/artifacts", {"name": name}, limit=100):
            ref = art.get("workflow_run") or {}
            if art.get("expired") or ref.get("head_sha") != pr.head_sha or not ref.get("id"):
                continue
            run_id = ref["id"]
            if run_id not in runs:
                try:
                    runs[run_id] = self.request("GET", f"/repos/{repo}/actions/runs/{run_id}")
                except GitHubError:
                    runs[run_id] = {}
            run = runs[run_id]
            if run.get("event") != "pull_request":
                continue
            if run.get("conclusion") not in (None, "success"):
                continue
            if workflow_path and run.get("path", "").split("@")[0] != workflow_path:
                continue
            head_repo = (run.get("head_repository") or {}).get("full_name")
            if head_repo != pr.head_repo or run.get("head_branch") != pr.head_ref:
                continue
            out.append(art)
        out.sort(key=lambda a: a.get("created_at") or "", reverse=True)
        return out

    def download_artifact(self, art: dict, max_bytes: int = 20_000_000) -> dict[str, bytes] | None:
        if art.get("size_in_bytes", 0) > max_bytes:
            log.warning("artifact %s is too large (%s bytes)", art.get("id"), art.get("size_in_bytes"))
            return None
        blob = self.request("GET", art["archive_download_url"], raw=True, binary=True)
        out = {}
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            total = 0
            for info in zf.infolist():
                total += info.file_size
                if total > max_bytes or len(out) > 20:
                    return None
                out[info.filename] = zf.read(info)
        return out

    def add_reaction(self, repo: str, comment_id: int, content: str = "eyes") -> None:
        try:
            self.request("POST", f"/repos/{repo}/issues/comments/{comment_id}/reactions", {"content": content})
        except GitHubError:
            pass


def _envelope_items(page: Any) -> list:
    """List endpoints that wrap results: {"total_count": n, "<key>": [...]} (check_runs, artifacts,
    workflow_runs, jobs, items...). Take the one list-valued key."""
    if not isinstance(page, dict):
        return []
    lists = [v for v in page.values() if isinstance(v, list)]
    return lists[0] if len(lists) == 1 else []


def _next_link(header: str) -> str | None:
    for part in header.split(","):
        section = part.split(";")
        if len(section) >= 2 and 'rel="next"' in section[1]:
            return section[0].strip()[1:-1]
    return None
