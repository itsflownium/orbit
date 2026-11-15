"""Core data types shared by checks, the reviewer and the commenter."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

BLOCKING = "blocking"
ADVISORY = "advisory"


@dataclass
class Finding:
    """One problem the bot found on a PR."""

    check: str  # name of the check that produced it, e.g. "ci"
    title: str  # one-line summary shown in the checklist
    severity: str = ADVISORY  # BLOCKING or ADVISORY
    detail: str = ""  # markdown shown under the title
    path: str | None = None  # file, for inline comments
    line: int | None = None  # line on the PR head (RIGHT side); last line of a range
    start_line: int | None = None  # first line of a multi-line range
    suggestion: str | None = None  # replacement text for a ```suggestion block
    fixable: bool = False  # an autofixer can resolve it
    key: str | None = None  # stable identity across runs; derived if not set
    recommendation: str = ""  # author-facing repair and verification guidance
    references: list[str] = field(default_factory=list)  # verified links to related source lines

    def __post_init__(self) -> None:
        if not self.key:
            raw = f"{self.check}|{self.title}|{self.path or ''}|{self.line or ''}"
            self.key = f"{self.check}-{hashlib.sha1(raw.encode()).hexdigest()[:8]}"

    @property
    def blocking(self) -> bool:
        return self.severity == BLOCKING


@dataclass
class ChangedFile:
    filename: str
    status: str  # added, modified, removed, renamed
    additions: int
    deletions: int
    patch: str | None = None
    previous_filename: str | None = None


@dataclass
class PullRequest:
    """The subset of PR data the bot needs, normalised from the GitHub API."""

    repo: str  # owner/name of the base repo
    number: int
    title: str
    body: str
    author: str
    author_association: str
    head_sha: str
    head_ref: str
    head_repo: str | None  # owner/name, None if the fork was deleted
    base_ref: str
    base_sha: str
    draft: bool
    mergeable: bool | None
    mergeable_state: str
    maintainer_can_modify: bool
    additions: int
    deletions: int
    changed_files: int
    created_at: str
    updated_at: str
    labels: list[str] = field(default_factory=list)
    files: list[ChangedFile] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_fork(self) -> bool:
        return self.head_repo != self.repo

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "PullRequest":
        head_repo = (data.get("head") or {}).get("repo") or None
        return cls(
            repo=data["base"]["repo"]["full_name"],
            number=data["number"],
            title=data.get("title") or "",
            body=data.get("body") or "",
            author=(data.get("user") or {}).get("login", ""),
            author_association=data.get("author_association", "NONE"),
            head_sha=data["head"]["sha"],
            head_ref=data["head"]["ref"],
            head_repo=head_repo["full_name"] if head_repo else None,
            base_ref=data["base"]["ref"],
            base_sha=data["base"]["sha"],
            draft=bool(data.get("draft")),
            mergeable=data.get("mergeable"),
            mergeable_state=data.get("mergeable_state") or "unknown",
            maintainer_can_modify=bool(data.get("maintainer_can_modify")),
            additions=data.get("additions", 0),
            deletions=data.get("deletions", 0),
            changed_files=data.get("changed_files", 0),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            labels=[lbl["name"] for lbl in data.get("labels", [])],
            raw=data,
        )


MAINTAINER_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
