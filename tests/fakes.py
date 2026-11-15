"""In-memory stand-in for prbot.github.GitHub."""

from __future__ import annotations

import copy

from prbot.github import GitHubError
from prbot.models import ChangedFile, PullRequest


def pr_data(number=1, repo="acme/widget", head_sha="a" * 40, author="alice", body="Adds a feature that does X because Y.",
            title="Add widget frobnication", mergeable=True, state="clean", fork=False, **extra):
    head_repo = f"{author}/widget" if fork else repo
    data = {
        "number": number, "state": "open", "title": title, "body": body, "draft": False,
        "user": {"login": author}, "author_association": "CONTRIBUTOR",
        "head": {"sha": head_sha, "ref": "feature", "repo": {"full_name": head_repo}},
        "base": {"sha": "b" * 40, "ref": "main", "repo": {"full_name": repo}},
        "mergeable": mergeable, "mergeable_state": state, "maintainer_can_modify": True,
        "additions": 10, "deletions": 0, "changed_files": 1, "created_at": "", "updated_at": "", "labels": [],
    }
    data.update(extra)
    return data


class FakeGitHub:
    def __init__(self):
        self.token = "fake"
        self.dry_run = False
        self.prs: dict[int, dict] = {}
        self.files: dict[int, list[ChangedFile]] = {}
        # most tests have no analysis workflow; tests of the lint gate set this back to required
        self.repo_files: dict[str, str] = {".github/prereview.yml": "analysis:\n  required: false\n"}
        self.check_runs_by_sha: dict[str, list[dict]] = {}
        self.statuses_by_sha: dict[str, list[dict]] = {}
        self.comments: dict[int, list[dict]] = {}
        self.reviews: list[dict] = []
        self.labels: dict[int, set[str]] = {}
        self.status_writes: list[dict] = []
        self.next_id = 100
        self.reject_lines: set[tuple[str, int]] = set()
        self.bot_login = "prbot-test[bot]"
        self.permissions: dict[str, str] = {}
        self.artifacts: dict[str, list[dict[str, bytes]]] = {}  # head_sha -> [files, ...]

    def login(self):
        return self.bot_login

    def permission(self, repo, user):
        return self.permissions.get(user, "read")

    def analysis_artifacts(self, repo, name, pr, workflow_path):
        return [{"id": i, "sha": pr.head_sha} for i, _ in enumerate(self.artifacts.get(pr.head_sha, []))]

    def download_artifact(self, art, max_bytes=20_000_000):
        return self.artifacts[art["sha"]][art["id"]]

    # reads
    def get_pr(self, repo, number, with_files=True):
        data = copy.deepcopy(self.prs[number])
        data["labels"] = [{"name": n} for n in sorted(self.labels.get(number, set()))]
        pr = PullRequest.from_api(data)
        pr.files = list(self.files.get(number, []))
        return pr

    def list_open_prs(self, repo, limit=None):
        return [self.prs[n] for n in sorted(self.prs)][:limit]

    def get_file_text(self, repo, path, ref):
        return self.repo_files.get(path)

    def check_runs(self, repo, sha):
        return self.check_runs_by_sha.get(sha, [])

    def combined_status(self, repo, sha):
        return {"statuses": self.statuses_by_sha.get(sha, [])}

    def job_log_tail(self, repo, job_id, lines=40):
        return "Error: test_widget failed\nassert 1 == 2"

    def issue_comments(self, repo, number):
        return list(self.comments.get(number, []))

    def graphql(self, query, variables):
        nodes = [{"number": n, "title": d["title"], "author": {"login": d["user"]["login"]},
                  "files": {"nodes": [{"path": f.filename} for f in self.files.get(n, [])]}}
                 for n, d in sorted(self.prs.items())]
        return {"data": {"repository": {"pullRequests": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                                                         "nodes": nodes}}}}

    def request(self, method, path, body=None, **kw):
        return []

    # writes
    def create_comment(self, repo, number, body, login=None):
        self.next_id += 1
        kind = "User" if login and login != self.bot_login else "Bot"
        c = {"id": self.next_id, "body": body, "user": {"login": login or self.bot_login, "type": kind}}
        self.comments.setdefault(number, []).append(c)
        return c

    def update_comment(self, repo, comment_id, body):
        for cs in self.comments.values():
            for c in cs:
                if c["id"] == comment_id:
                    c["body"] = body
                    return c
        raise GitHubError(404, "Not Found", f"/comments/{comment_id}")

    def get_labels(self, repo, number):
        return set(self.labels.get(number, set()))

    def create_review(self, repo, number, sha, comments, body=""):
        for c in comments:
            if (c["path"], c["line"]) in self.reject_lines:
                raise GitHubError(422, "line not in diff", "/reviews")
        self.reviews.append({"number": number, "comments": comments})
        return {}

    def ensure_label(self, repo, name, color, description):
        pass

    def add_labels(self, repo, number, labels):
        self.labels.setdefault(number, set()).update(labels)

    def remove_label(self, repo, number, label):
        self.labels.setdefault(number, set()).discard(label)

    def set_status(self, repo, sha, state, context, description, url=None):
        self.status_writes.append({"sha": sha, "state": state, "context": context, "description": description})

    def add_reaction(self, repo, comment_id, content="eyes"):
        pass

    def summary(self, number):
        from prbot.commenter import MARKER
        return next(c["body"] for c in self.comments.get(number, [])
                    if MARKER in c["body"] and c["user"]["login"] == self.bot_login)
