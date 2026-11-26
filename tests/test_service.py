"""Unit tests for contribution ranking rules."""

import threading
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
from contribution_radar.service import ContributionRadar, match_label, normalize_label

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
        self.extra_labels: dict[str, list[str]] = {}
        self.issue_requests: list[tuple[str, str]] = []
        self.pull_request_requests: list[str] = []

    def get_repository(self, name: str) -> Repository:
        """Return canned repository metadata, or raise a configured failure."""

        if name in self.failures:
            raise self.failures[name]
        return self.repositories[name]

    def list_labels(self, name: str) -> list[str]:
        """Return every label used by the canned issues, plus any extras."""

        used = {label for issue in self.issues.get(name, []) for label in issue.labels}
        return sorted(used | set(self.extra_labels.get(name, [])))

    def list_open_issues(self, name: str, label: str) -> list[Issue]:
        """Return canned issues carrying the label, as GitHub's label filter does."""

        self.issue_requests.append((name, label))
        return [issue for issue in self.issues.get(name, []) if label in issue.labels]

    def list_open_pull_requests(self, name: str) -> list[PullRequest]:
        """Return canned pull requests."""

        self.pull_request_requests.append(name)
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
    assert result.repositories[1].detail == "1 open item under 1 label checked, 1 eligible"
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


PROJECT = Repository("owner/project", "https://github.com/owner/project", 10_000, False)


@pytest.mark.parametrize(
    "label,normalized",
    [
        ("Good First Issue", "good first issue"),
        ("good-first-issue", "good first issue"),
        ("good_first_issue", "good first issue"),
        ("good first issue 🐤", "good first issue"),
        ("help wanted 🆘", "help wanted"),
        ("good first issue :baby_chick:", "good first issue"),
        ("help wanted :sos:", "help wanted"),
        ("kind/help-wanted", "kind/help wanted"),
        ("  Help   Wanted ", "help wanted"),
    ],
)
def test_normalize_label_ignores_case_separators_and_emoji(label: str, normalized: str) -> None:
    """Treat common spellings of the same label as equal."""

    assert normalize_label(label) == normalized


@pytest.mark.parametrize(
    "label,key",
    [
        ("kind/help-wanted", "help wanted"),
        ("exp/good-first-issue", "good first issue"),
        ("good first issue 🐤", "good first issue"),
        ("not help wanted", None),
        ("help wanted later", None),
        ("contributor/wanted", None),
    ],
)
def test_match_label_accepts_prefixes_but_not_partial_words(label: str, key: str | None) -> None:
    """Match whole labels or labels after a prefix, never fragments."""

    assert match_label(label, ["good first issue", "help wanted"]) == key


def test_scan_fetches_only_contribution_labels_and_deduplicates() -> None:
    """Query each matching label once and count shared issues once."""

    gateway = FakeGateway(
        repositories={"owner/project": PROJECT},
        issues={
            "owner/project": [
                make_issue(1, labels=("good-first-issue 🐤", "kind/help-wanted", "kind/bug")),
                make_issue(2, labels=("kind/help-wanted",)),
                make_issue(3, labels=("question",)),
            ]
        },
    )

    result = ContributionRadar(gateway).scan([RepositoryTarget("owner/project")], now=NOW)

    assert sorted(label for _, label in gateway.issue_requests) == [
        "good-first-issue 🐤",
        "kind/help-wanted",
    ]
    assert [candidate.issue.number for candidate in result.candidates] == [1, 2]
    assert result.candidates[0].reasons == (
        "label: bug",
        "label: good first issue",
        "label: help wanted",
    )
    assert result.repositories[0].detail == "2 open items under 2 labels checked, 2 eligible"


def test_scan_uses_labels_configured_for_the_repository() -> None:
    """Accept a project's own invitation label when the target lists it."""

    gateway = FakeGateway(
        repositories={"owner/project": PROJECT},
        issues={"owner/project": [make_issue(5, labels=("contributor/wanted",))]},
    )
    radar = ContributionRadar(gateway)

    default = radar.scan([RepositoryTarget("owner/project")], now=NOW)
    configured = radar.scan(
        [RepositoryTarget("owner/project", labels=("Contributor/Wanted",))], now=NOW
    )

    assert default.candidates == ()
    assert default.repositories[0].detail == "no contribution labels defined"
    assert [candidate.issue.number for candidate in configured.candidates] == [5]
    assert configured.candidates[0].reasons == ("label: contributor/wanted",)


def test_scan_skips_pull_request_lookup_without_labeled_issues() -> None:
    """Avoid the pull request request when no labeled issue is open."""

    gateway = FakeGateway(repositories={"owner/project": PROJECT})
    gateway.extra_labels = {"owner/project": ["help wanted"]}

    result = ContributionRadar(gateway).scan([RepositoryTarget("owner/project")], now=NOW)

    assert gateway.issue_requests == [("owner/project", "help wanted")]
    assert gateway.pull_request_requests == []
    assert result.repositories[0].detail == "0 open items under 1 label checked, 0 eligible"


def test_parallel_scan_reports_in_configuration_order() -> None:
    """Report completion order live, but return results in config order."""

    first_may_finish = threading.Event()

    class SlowFirstGateway(FakeGateway):
        """Hold the first repository until the second has been reported."""

        def get_repository(self, name: str) -> Repository:
            """Block the first repository until released."""

            if name == "owner/first":
                assert first_may_finish.wait(timeout=5)
            return super().get_repository(name)

    gateway = SlowFirstGateway(
        repositories={
            "owner/first": Repository("owner/first", "https://x.test/1", 10_000, False),
            "owner/second": Repository("owner/second", "https://x.test/2", 10_000, False),
        },
        issues={"owner/first": [make_issue(1)], "owner/second": [make_issue(2)]},
    )
    observed: list[str] = []

    def observe(scan: RepositoryScan) -> None:
        """Record completion order and release the first repository."""

        observed.append(scan.name)
        first_may_finish.set()

    result = ContributionRadar(gateway).scan(
        [RepositoryTarget("owner/first"), RepositoryTarget("owner/second")],
        now=NOW,
        on_repository=observe,
        max_workers=2,
    )

    assert observed == ["owner/second", "owner/first"]
    assert [scan.name for scan in result.repositories] == ["owner/first", "owner/second"]


def test_parallel_scan_runs_repositories_concurrently_and_isolates_failures() -> None:
    """Scan several repositories at once, keeping each failure to its own row."""

    all_started = threading.Barrier(3, timeout=5)

    class ConcurrentGateway(FakeGateway):
        """Proceed only once three repositories are in flight together."""

        def get_repository(self, name: str) -> Repository:
            """Wait for the other workers, then answer or fail."""

            all_started.wait()
            return super().get_repository(name)

    gateway = ConcurrentGateway(
        repositories={
            name: Repository(name, f"https://x.test/{name}", 10_000, False)
            for name in ("owner/a", "owner/c")
        },
        issues={"owner/a": [make_issue(1)], "owner/c": [make_issue(3)]},
    )
    gateway.failures = {"owner/b": GitHubApiError("HTTP Error 502")}

    result = ContributionRadar(gateway).scan(
        [RepositoryTarget(name) for name in ("owner/a", "owner/b", "owner/c")],
        now=NOW,
        max_workers=3,
    )

    assert [(scan.name, scan.status) for scan in result.repositories] == [
        ("owner/a", ScanStatus.SCANNED),
        ("owner/b", ScanStatus.FAILED),
        ("owner/c", ScanStatus.SCANNED),
    ]
    assert sorted(candidate.issue.number for candidate in result.candidates) == [1, 3]


@pytest.mark.parametrize("workers", [0, 17])
def test_scan_rejects_invalid_worker_counts(workers: int) -> None:
    """Reject concurrency outside the supported range."""

    with pytest.raises(ValueError, match="max_workers"):
        ContributionRadar(FakeGateway(repositories={})).scan([], now=NOW, max_workers=workers)
