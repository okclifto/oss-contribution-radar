"""Typed domain models for contribution discovery."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


@dataclass(frozen=True, slots=True)
class RepositoryTarget:
    """A public repository included in the daily discovery scan."""

    name: str
    minimum_stars: int = 1_000
    labels: tuple[str, ...] = ()
    """Extra labels this project uses to invite contributors, e.g. ``contributor/wanted``."""


@dataclass(frozen=True, slots=True)
class Repository:
    """Repository metadata used to assess project popularity and activity."""

    name: str
    url: str
    stars: int
    archived: bool
    pushed_at: datetime | None = None
    fork: bool = False
    disabled: bool = False


@dataclass(frozen=True, slots=True)
class Issue:
    """A normalized open issue returned by GitHub."""

    repository: str
    number: int
    title: str
    url: str
    labels: tuple[str, ...]
    assignees: tuple[str, ...]
    comments: int
    created_at: datetime
    updated_at: datetime
    is_pull_request: bool = False
    body: str = ""


@dataclass(frozen=True, slots=True)
class PullRequest:
    """An open pull request used to detect work already underway."""

    title: str
    body: str


@dataclass(frozen=True, slots=True)
class Candidate:
    """A ranked, review-gated open-source contribution opportunity."""

    repository: Repository
    issue: Issue
    score: float
    reasons: tuple[str, ...]


class ScanStatus(StrEnum):
    """Outcome of scanning one configured repository."""

    SCANNED = "scanned"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RepositoryScan:
    """What happened when one repository target was scanned."""

    name: str
    status: ScanStatus
    detail: str
    eligible: int = 0


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Ranked candidates plus a per-repository record of the scan."""

    candidates: tuple[Candidate, ...]
    repositories: tuple[RepositoryScan, ...]

    def count(self, status: ScanStatus) -> int:
        """Return how many repositories finished with the given status."""

        return sum(1 for scan in self.repositories if scan.status is status)
