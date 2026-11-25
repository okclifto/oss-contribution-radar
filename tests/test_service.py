"""Unit tests for contribution ranking rules."""

from datetime import UTC, datetime, timedelta

import pytest

from contribution_radar.github_client import GitHubApiError
from contribution_radar.models import (
    Issue,
    PullRequest,
    Repository,
    RepositoryScan,
    RepositoryTarget,
    ScanStatus,
)
from contribution_radar.service import ContributionRadar

NOW = datetime(2026, 9, 16, tzinfo=UTC)


class FakeGateway:
    """In-memory repository gateway for deterministic service tests."""

    def __init__(
        self,
        repositories: dict[str, Repository],
        issues: dict[str, list[Issue]] | None = None,
        pull_requests: dict[str, list[PullRequest]] | None = None,
    ) -> None:
        """Store canned responses by repository name."""

        self.repositories = repositories
        self.issues = issues or {}
        self.pull_requests = pull_requests or {}
        self.failures: dict[str, Exception] = {}

    def get_repository(self, name: str) -> Repository:
        """Return canned repository metadata, or raise a configured failure."""

        if name in self.failures:
            raise self.failures[name]
        return self.repositories[name]

    def list_open_issues(self, name: str) -> list[Issue]:
        """Return canned issues."""

        return self.issues.get(name, [])

    def list_open_pull_requests(self, name: str) -> list[PullRequest]:
        """Return canned pull requests."""

        return self.pull_requests.get(name, [])


def make_issue(
    number: int,
    *,
    labels: tuple[str, ...] = ("help wanted",),
    assignees: tuple[str, ...] = (),
    is_pull_request: bool = False,
    updated_days_ago: int = 1,
    comments: int = 0,
) -> Issue:
    """Build a representative issue with overridable eligibility fields."""

    return Issue(
        repository="owner/project",
        number=number,
        title=f"Issue {number}",
        url=f"https://github.com/owner/project/issues/{number}",
        labels=labels,
        assignees=assignees,
        comments=comments,
        created_at=NOW - timedelta(days=30),
        updated_at=NOW - timedelta(days=updated_days_ago),
        is_pull_request=is_pull_request,
    )


def test_discover_ranks_eligible_unassigned_issues() -> None:
    """Prefer strongly labeled, fresh issues and return stable ordering."""

    repository = Repository(
        name="owner/project",
        url="https://github.com/owner/project",
        stars=10_000,
        archived=False,
    )
    gateway = FakeGateway(
        repositories={"owner/project": repository},
        issues={
            "owner/project": [
                make_issue(1, labels=("good first issue", "bug")),
                make_issue(2, labels=("help wanted",), updated_days_ago=60, comments=8),
            ]
        },
    )

    candidates = ContributionRadar(gateway).discover(
        [RepositoryTarget("owner/project")],
        now=NOW,
    )

    assert [candidate.issue.number for candidate in candidates] == [1, 2]
    assert candidates[0].score > candidates[1].score
    assert candidates[0].reasons == ("label: bug", "label: good first issue")


def test_discover_filters_non_actionable_work() -> None:
    """Exclude assigned items, PRs, weak labels, and issues with competing PRs."""

    repository = Repository(
        name="owner/project",
        url="https://github.com/owner/project",
        stars=5_000,
        archived=False,
    )
    gateway = FakeGateway(
        repositories={"owner/project": repository},
        issues={
            "owner/project": [
                make_issue(1, assignees=("maintainer",)),
                make_issue(2, is_pull_request=True),
                make_issue(3, labels=("question",)),
                make_issue(4),
            ]
        },
        pull_requests={
            "owner/project": [
                PullRequest(title="Implement requested behavior", body="Fixes owner/project#4")
            ]
        },
    )

    candidates = ContributionRadar(gateway).discover(
        [RepositoryTarget("owner/project")],
        now=NOW,
    )

    assert candidates == []


def test_discover_skips_archived_and_small_repositories() -> None:
    """Do not recommend work from archived or insufficiently popular projects."""

    gateway = FakeGateway(
        repositories={
            "owner/archived": Repository(
                "owner/archived", "https://example.test/archived", 20_000, True
            ),
            "owner/small": Repository("owner/small", "https://example.test/small", 99, False),
        }
    )

    candidates = ContributionRadar(gateway).discover(
        [
            RepositoryTarget("owner/archived"),
            RepositoryTarget("owner/small", minimum_stars=100),
        ],
        now=NOW,
    )

    assert candidates == []


@pytest.mark.parametrize("limit", [0, 51])
def test_discover_rejects_invalid_limits(limit: int) -> None:
    """Reject limits outside the public CLI contract."""

    radar = ContributionRadar(FakeGateway(repositories={}))

    with pytest.raises(ValueError, match="between 1 and 50"):
        radar.discover([], limit=limit, now=NOW)


def test_scan_isolates_repository_failures() -> None:
    """Keep scanning after one repository errors and record each outcome in order."""

    healthy = Repository("owner/healthy", "https://github.com/owner/healthy", 10_000, False)
    gateway = FakeGateway(
        repositories={"owner/healthy": healthy},
        issues={"owner/healthy": [make_issue(7), make_issue(8, labels=("question",))]},
    )
    gateway.failures = {
        "owner/rate-limited": GitHubApiError("HTTP Error 403: rate limit exceeded"),
        "bad name": ValueError("invalid repository name: 'bad name'"),
    }
    observed: list[RepositoryScan] = []

    result = ContributionRadar(gateway).scan(
        [
            RepositoryTarget("owner/rate-limited"),
            RepositoryTarget("owner/healthy"),
            RepositoryTarget("bad name"),
        ],
        now=NOW,
        on_repository=observed.append,
    )

    assert [candidate.issue.number for candidate in result.candidates] == [7]
    assert observed == list(result.repositories)
    assert [(scan.name, scan.status) for scan in result.repositories] == [
        ("owner/rate-limited", ScanStatus.FAILED),
        ("owner/healthy", ScanStatus.SCANNED),
        ("bad name", ScanStatus.FAILED),
    ]
    assert "rate limit exceeded" in result.repositories[0].detail
    assert result.repositories[1].detail == "2 open items checked, 1 eligible"
    assert result.repositories[1].eligible == 1
    assert result.count(ScanStatus.FAILED) == 2


def test_scan_records_why_repositories_were_skipped() -> None:
    """Explain archived and below-threshold skips."""

    gateway = FakeGateway(
        repositories={
            "owner/archived": Repository("owner/archived", "https://x.test/a", 20_000, True),
            "owner/small": Repository("owner/small", "https://x.test/s", 99, False),
        }
    )

    result = ContributionRadar(gateway).scan(
        [RepositoryTarget("owner/archived"), RepositoryTarget("owner/small", 1_500)],
        now=NOW,
    )

    assert [(scan.status, scan.detail) for scan in result.repositories] == [
        (ScanStatus.SKIPPED, "archived"),
        (ScanStatus.SKIPPED, "99 stars is below the 1,500 minimum"),
    ]
