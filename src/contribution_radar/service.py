"""Business rules for selecting worthwhile contribution opportunities."""

from __future__ import annotations

import math
import re
from collections import Counter
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

# Keys are normalized label names (see normalize_label). An issue scores its single
# strongest contribution label: labels that mean the same thing do not add up.
CONTRIBUTION_LABEL_WEIGHTS = {
    "good first issue": 25.0,
    "good first contribution": 25.0,
    "first timers only": 25.0,
    "help wanted": 8.0,
    "contributions welcome": 8.0,
}
CONFIGURED_LABEL_WEIGHT = 20.0
# A bare "help wanted" is often a hard problem; an extra beginner label says otherwise.
BEGINNER_HINT_LABELS = ("easy", "beginner", "beginner friendly", "starter", "low hanging fruit")
BEGINNER_HINT_WEIGHT = 19.0
QUALITY_LABEL_WEIGHTS = {
    "bug": 3.0,
    "documentation": 3.0,
    "docs": 3.0,
}
DOCS_LABELS = ("documentation", "docs")
# Labels that mean the issue is not ready for anyone to pick up. "security" is
# deliberately absent: many targets are security tools, where it marks the topic.
NOT_READY_LABELS = (
    "blocked",
    "needs triage",
    "needs design",
    "needs decision",
    "needs info",
    "needs more info",
    "needs information",
    "needs reproduction",
    "needs repro",
    "waiting for author",
    "waiting on author",
    "on hold",
    "stale",
    "discussion",
    "question",
    "wontfix",
    "wont fix",
    "duplicate",
    "invalid",
    "rfc",
    "epic",
    "breaking change",
)
SMALL_SIZE_LABELS = (
    "size/xs",
    "size/s",
    "effort low",
    "effort/low",
    "difficulty easy",
    "difficulty/easy",
)
LARGE_SIZE_LABELS = (
    "size/l",
    "size/xl",
    "size/xxl",
    "hard",
    "complex",
    "effort high",
    "effort/high",
    "difficulty hard",
    "difficulty/hard",
)
SMALL_SIZE_BONUS = 10.0
LARGE_SIZE_PENALTY = -15.0

# Hard limits: older or busier issues are excluded rather than down-ranked.
MAX_UPDATED_AGE_DAYS = 180
MAX_COMMENTS = 15

# Stars reward contributions to big projects. The log scale runs from 0 points at
# POPULARITY_FLOOR_STARS to POPULARITY_MAX at POPULARITY_FULL_STARS and above.
POPULARITY_MAX = 20.0
POPULARITY_FLOOR_STARS = 2_000
POPULARITY_FULL_STARS = 100_000

# Ranking limits applied after scoring.
MINIMUM_SCORE = 40.0
MAX_PER_REPOSITORY = 2
MAX_DOCS_ONLY = 3

SHORT_BODY_CHARACTERS = 200

_SHORTCODE = re.compile(r":[a-z0-9_+-]+:")
_SEPARATORS = re.compile(r"[-_\s]+")
_DECORATION = re.compile(r"[^\w\s/-]")
_META_TITLE = re.compile(
    r"^\W*(?:tracking|umbrella|roadmap|rfc|epic|proposal|meta)\b|\b(?:tracking|umbrella) issue\b",
    re.IGNORECASE,
)
_EASY_TITLE = re.compile(r"\b(?:typos?|error messages?|add (?:a )?tests?|rename|lint)\b", re.I)
_HARD_TITLE = re.compile(
    r"\b(?:refactor\w*|redesign|rewrite|performance|concurrency|race|migrat\w*)\b", re.I
)
_FILE_PATH = re.compile(
    r"\b[\w.-]+/[\w./-]*\.\w{1,5}\b"
    r"|\b\w+\.(?:py|go|rs|ts|tsx|js|jsx|java|rb|md|ya?ml|toml|json|c|h|cpp|sh)\b"
)
_REPRO = re.compile(
    r"steps to reproduce|to reproduce|reproduction steps|expected (?:behaviou?r|result|output)",
    re.IGNORECASE,
)


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
                -candidate.issue.updated_at.timestamp(),
                candidate.issue.comments,
                candidate.repository.name.casefold(),
                candidate.issue.number,
            )
        )
        return ScanResult(candidates=self._select(candidates, limit), repositories=tuple(scans))

    @staticmethod
    def _select(ranked: list[Candidate], limit: int) -> tuple[Candidate, ...]:
        """Take the best candidates, capping each repository and docs-only picks.

        Candidates below ``MINIMUM_SCORE`` are never shown, so a weak day publishes
        fewer than ``limit`` picks rather than padding the list.
        """

        picked: list[Candidate] = []
        per_repository: Counter[str] = Counter()
        docs_only = 0
        for candidate in ranked:
            if candidate.score < MINIMUM_SCORE or len(picked) == limit:
                break
            repository = candidate.repository.name.casefold()
            if per_repository[repository] >= MAX_PER_REPOSITORY:
                continue
            is_docs_only = _is_docs_only(candidate.issue)
            if is_docs_only and docs_only >= MAX_DOCS_ONLY:
                continue
            picked.append(candidate)
            per_repository[repository] += 1
            docs_only += is_docs_only
        return tuple(picked)

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
            if repository.disabled:
                return [], RepositoryScan(target.name, ScanStatus.SKIPPED, "disabled")
            if repository.fork:
                return [], RepositoryScan(target.name, ScanStatus.SKIPPED, "fork")
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
        """Apply the hard filters, then score one issue."""

        if issue.is_pull_request or issue.assignees:
            return None
        contribution_labels = self._matched(issue.labels, contribution_weights)
        if not contribution_labels:
            return None
        updated_days = _days_between(issue.updated_at, now)
        if updated_days > MAX_UPDATED_AGE_DAYS or issue.comments > MAX_COMMENTS:
            return None
        if self._matched(issue.labels, NOT_READY_LABELS) or _META_TITLE.search(issue.title):
            return None
        if self._has_competing_pull_request(repository.name, issue, pull_requests):
            return None

        quality_labels = self._matched(issue.labels, QUALITY_LABEL_WEIGHTS)
        small_labels = self._matched(issue.labels, SMALL_SIZE_LABELS)
        large_labels = self._matched(issue.labels, LARGE_SIZE_LABELS)
        body_score, body_reasons = self._body_score(issue.body)

        score = (
            self._label_score(issue, contribution_labels, contribution_weights)
            + sum(QUALITY_LABEL_WEIGHTS[label] for label in quality_labels)
            + self._freshness_score(updated_days)
            + self._discussion_score(issue.comments)
            + self._vitality_score(repository, now)
            + self._popularity_score(repository.stars)
            + (SMALL_SIZE_BONUS if small_labels else 0.0)
            + (LARGE_SIZE_PENALTY if large_labels else 0.0)
            + self._title_score(issue.title)
            + self._age_score(issue, now)
            + body_score
        )
        labels = sorted(contribution_labels | quality_labels | small_labels)
        reasons = [f"label: {label}" for label in labels]
        reasons.append(f"updated {self._plural(int(updated_days), 'day')} ago")
        reasons.extend(body_reasons)
        return Candidate(
            repository=repository,
            issue=issue,
            score=round(score, 1),
            reasons=tuple(reasons),
        )

    @classmethod
    def _label_score(
        cls,
        issue: Issue,
        contribution_labels: set[str],
        contribution_weights: dict[str, float],
    ) -> float:
        """Score the strongest contribution label, lifted by a beginner hint."""

        strongest = max(contribution_weights[label] for label in contribution_labels)
        if cls._matched(issue.labels, BEGINNER_HINT_LABELS):
            return max(strongest, BEGINNER_HINT_WEIGHT)
        return strongest

    @staticmethod
    def _freshness_score(updated_days: float) -> float:
        """Reward recent activity: the issue is more likely to still be real."""

        if updated_days <= 30:
            return 25.0
        if updated_days <= 90:
            return 20.0
        return 10.0

    @staticmethod
    def _discussion_score(comments: int) -> float:
        """Prefer a little confirmation over silence or a long, contested thread."""

        if 1 <= comments <= 4:
            return 15.0
        if comments <= 8:
            return 8.0
        return 2.0

    @staticmethod
    def _vitality_score(repository: Repository, now: datetime) -> float:
        """Reward repositories pushed to recently, where a PR is likelier to be reviewed."""

        if repository.pushed_at is None:
            return 0.0
        pushed_days = _days_between(repository.pushed_at, now)
        if pushed_days <= 14:
            return 10.0
        if pushed_days <= 60:
            return 5.0
        return 0.0

    @staticmethod
    def _popularity_score(stars: int) -> float:
        """Reward bigger projects on a log scale between the floor and full stars."""

        low = math.log10(POPULARITY_FLOOR_STARS)
        high = math.log10(POPULARITY_FULL_STARS)
        fraction = (math.log10(max(stars, 1)) - low) / (high - low)
        return POPULARITY_MAX * min(max(fraction, 0.0), 1.0)

    @staticmethod
    def _title_score(title: str) -> float:
        """Use the title as a small, unreliable hint about scope and clarity."""

        score = 0.0
        if _EASY_TITLE.search(title):
            score += 5.0
        if _HARD_TITLE.search(title):
            score -= 5.0
        stripped = title.strip()
        if len(stripped.split()) < 4 or stripped.endswith(("?", "...", "\u2026")):
            score -= 5.0
        return min(max(score, -5.0), 5.0)

    @staticmethod
    def _age_score(issue: Issue, now: datetime) -> float:
        """Penalize issues too new to be triaged, or so old they are likely obsolete."""

        created_days = _days_between(issue.created_at, now)
        if created_days > 730:
            return -5.0
        if created_days < 7 and issue.comments == 0:
            return -3.0
        return 0.0

    @staticmethod
    def _body_score(body: str) -> tuple[float, list[str]]:
        """Reward a body that shows a newcomer where to start."""

        score = 0.0
        reasons: list[str] = []
        if "```" in body or _FILE_PATH.search(body):
            score += 5.0
            reasons.append("points to code")
        if _REPRO.search(body):
            score += 5.0
            reasons.append("has repro steps")
        if len(body.strip()) < SHORT_BODY_CHARACTERS:
            score -= 5.0
        return score, reasons

    @staticmethod
    def _matched(labels: Iterable[str], keys: Iterable[str]) -> set[str]:
        """Return the keys that an issue's labels match."""

        keys = tuple(keys)
        return {key for label in labels if (key := match_label(label, keys)) is not None}

    @staticmethod
    def _has_competing_pull_request(
        repository: str,
        issue: Issue,
        pull_requests: list[PullRequest],
    ) -> bool:
        """Detect open pull requests that reference the issue.

        A bare ``#N``, ``owner/repo#N``, the issue URL, or a closing keyword before the
        number all count: any of them suggests someone is already working on it.
        """

        number = issue.number
        pattern = re.compile(
            rf"(?<![\w/#])#{number}\b"
            rf"|\b{re.escape(repository)}#{number}\b"
            rf"|{re.escape(issue.url)}\b"
            rf"|\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#?{number}\b",
            re.IGNORECASE,
        )
        return any(
            pattern.search(f"{pull_request.title}\n{pull_request.body}")
            for pull_request in pull_requests
        )


def _days_between(earlier: datetime, now: datetime) -> float:
    """Return the non-negative number of days from ``earlier`` to ``now``."""

    return max((now - earlier).total_seconds() / 86_400, 0.0)


def _is_docs_only(issue: Issue) -> bool:
    """Return whether an issue is documentation work with no bug label."""

    labels = issue.labels
    has_docs = any(match_label(label, DOCS_LABELS) for label in labels)
    return has_docs and not any(match_label(label, ("bug",)) for label in labels)
