"""Business rules for selecting worthwhile contribution opportunities."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime

from contribution_radar.models import (
    Candidate,
    Issue,
    PullRequest,
    Repository,
    RepositoryScan,
    RepositoryTarget,
    ScanResult,
    ScanStatus,
)
from contribution_radar.ports import RepositoryGateway, RepositoryGatewayError

ScanObserver = Callable[[RepositoryScan], None]
MAX_WORKERS = 16

# Keys are normalized label names (see normalize_label).
CONTRIBUTION_LABEL_WEIGHTS = {
    "good first issue": 55.0,
    "help wanted": 40.0,
    "good first contribution": 55.0,
    "contributions welcome": 35.0,
}
CONFIGURED_LABEL_WEIGHT = 40.0
QUALITY_LABEL_WEIGHTS = {
    "bug": 15.0,
    "documentation": 12.0,
    "docs": 12.0,
}

_SHORTCODE = re.compile(r":[a-z0-9_+-]+:")
_SEPARATORS = re.compile(r"[-_\s]+")
_DECORATION = re.compile(r"[^\w\s/-]")


def normalize_label(label: str) -> str:
    """Reduce a label to a comparable form.

    Case, hyphens, underscores, emoji (including ``:shortcode:`` text) and
    punctuation are ignored, so ``Good-First-Issue``, ``good first issue 🐤`` and
    ``good first issue :baby_chick:`` all become ``good first issue``. A
    ``prefix/`` such as ``kind/`` is kept here and handled by ``match_label``.
    """

    without_emoji = _SHORTCODE.sub("", label.casefold())
    without_decoration = _DECORATION.sub("", without_emoji)
    return _SEPARATORS.sub(" ", without_decoration).strip()


def match_label(label: str, keys: Iterable[str]) -> str | None:
    """Return the key a label matches, directly or after a ``prefix/``."""

    normalized = normalize_label(label)
    for key in keys:
        if normalized == key or normalized.endswith(f"/{key}"):
            return key
    return None


class ContributionRadar:
    """Rank review-ready issues without creating upstream activity."""

    def __init__(self, gateway: RepositoryGateway) -> None:
        """Initialize the service with a read-only repository gateway."""

        self._gateway = gateway

    def discover(
        self,
        targets: list[RepositoryTarget],
        limit: int = 10,
        now: datetime | None = None,
    ) -> list[Candidate]:
        """Return the highest-quality unassigned opportunities."""

        return list(self.scan(targets, limit=limit, now=now).candidates)

    def scan(
        self,
        targets: list[RepositoryTarget],
        limit: int = 10,
        now: datetime | None = None,
        on_repository: ScanObserver | None = None,
        max_workers: int = 1,
    ) -> ScanResult:
        """Scan every target, isolating failures so one repository cannot abort the run.

        Up to ``max_workers`` repositories are scanned at once, each on its own
        thread; the gateway must therefore be safe to share between threads.
        ``on_repository`` is called on the calling thread as each repository
        finishes, in completion order. The returned result is in configuration
        order regardless, so reports do not depend on network timing.
        """

        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        if not 1 <= max_workers <= MAX_WORKERS:
            raise ValueError(f"max_workers must be between 1 and {MAX_WORKERS}")

        evaluated_at = now or datetime.now(UTC)
        outcomes: dict[int, tuple[list[Candidate], RepositoryScan]] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(self._scan_repository, target, evaluated_at): index
                for index, target in enumerate(targets)
            }
            for future in as_completed(futures):
                outcome = future.result()
                outcomes[futures[future]] = outcome
                if on_repository is not None:
                    on_repository(outcome[1])

        ordered = [outcomes[index] for index in range(len(targets))]
        candidates = [candidate for found, _ in ordered for candidate in found]
        scans = [scan for _, scan in ordered]

        candidates.sort(
            key=lambda candidate: (
                -candidate.score,
                candidate.repository.name.casefold(),
                candidate.issue.number,
            )
        )
        return ScanResult(candidates=tuple(candidates[:limit]), repositories=tuple(scans))

    def _scan_repository(
        self,
        target: RepositoryTarget,
        now: datetime,
    ) -> tuple[list[Candidate], RepositoryScan]:
        """Scan one repository, converting data-access errors into a failed outcome."""

        try:
            repository = self._gateway.get_repository(target.name)
            if repository.archived:
                return [], RepositoryScan(target.name, ScanStatus.SKIPPED, "archived")
            if repository.stars < target.minimum_stars:
                return [], RepositoryScan(
                    target.name,
                    ScanStatus.SKIPPED,
                    f"{repository.stars:,} stars is below the {target.minimum_stars:,} minimum",
                )

            weights = self._contribution_weights(target)
            labels = [
                label
                for label in self._gateway.list_labels(target.name)
                if match_label(label, weights)
            ]
            if not labels:
                return [], RepositoryScan(
                    target.name, ScanStatus.SCANNED, "no contribution labels defined"
                )

            # An issue with several matching labels comes back once per label.
            issues: dict[int, Issue] = {}
            for label in labels:
                for issue in self._gateway.list_open_issues(target.name, label):
                    issues[issue.number] = issue
            pull_requests = self._gateway.list_open_pull_requests(target.name) if issues else []
        except (RepositoryGatewayError, ValueError) as error:
            return [], RepositoryScan(target.name, ScanStatus.FAILED, str(error))

        candidates = [
            candidate
            for issue in issues.values()
            if (candidate := self._evaluate(repository, issue, weights, pull_requests, now))
            is not None
        ]
        return candidates, RepositoryScan(
            target.name,
            ScanStatus.SCANNED,
            f"{self._plural(len(issues), 'open item')} under "
            f"{self._plural(len(labels), 'label')} checked, "
            f"{len(candidates)} eligible",
            eligible=len(candidates),
        )

    @staticmethod
    def _contribution_weights(target: RepositoryTarget) -> dict[str, float]:
        """Combine the default contribution labels with the target's own."""

        weights = dict(CONTRIBUTION_LABEL_WEIGHTS)
        for label in target.labels:
            weights.setdefault(normalize_label(label), CONFIGURED_LABEL_WEIGHT)
        return weights

    @staticmethod
    def _plural(count: int, noun: str) -> str:
        """Format a count with a simply pluralized noun."""

        return f"{count} {noun}" if count == 1 else f"{count} {noun}s"

    def _evaluate(
        self,
        repository: Repository,
        issue: Issue,
        contribution_weights: dict[str, float],
        pull_requests: list[PullRequest],
        now: datetime,
    ) -> Candidate | None:
        """Apply eligibility checks and score one issue."""

        if issue.is_pull_request or issue.assignees:
            return None

        contribution_labels = self._matched(issue.labels, contribution_weights)
        if not contribution_labels:
            return None
        if self._has_competing_pull_request(repository.name, issue.number, pull_requests):
            return None

        quality_labels = self._matched(issue.labels, QUALITY_LABEL_WEIGHTS)
        reasons = [f"label: {label}" for label in sorted(contribution_labels | quality_labels)]
        label_score = sum(contribution_weights[label] for label in contribution_labels)
        quality_score = sum(QUALITY_LABEL_WEIGHTS[label] for label in quality_labels)
        popularity_score = min(math.log10(max(repository.stars, 1)) * 10.0, 60.0)
        age_days = max((now - issue.updated_at).total_seconds() / 86_400, 0.0)
        freshness_score = max(30.0 - age_days / 6.0, 0.0)
        discussion_penalty = min(issue.comments, 20) * 0.5

        return Candidate(
            repository=repository,
            issue=issue,
            score=round(
                label_score
                + quality_score
                + popularity_score
                + freshness_score
                - discussion_penalty,
                1,
            ),
            reasons=tuple(reasons),
        )

    @staticmethod
    def _matched(labels: Iterable[str], weights: dict[str, float]) -> set[str]:
        """Return the weighted keys that an issue's labels match."""

        return {key for label in labels if (key := match_label(label, weights)) is not None}

    @staticmethod
    def _has_competing_pull_request(
        repository: str,
        issue_number: int,
        pull_requests: list[PullRequest],
    ) -> bool:
        """Detect open pull requests that claim to close the issue."""

        escaped_repository = re.escape(repository)
        pattern = re.compile(
            rf"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+"
            rf"(?:{escaped_repository})?#?{issue_number}\b",
            re.IGNORECASE,
        )
        return any(
            pattern.search(f"{pull_request.title}\n{pull_request.body}")
            for pull_request in pull_requests
        )
