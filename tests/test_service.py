"""Unit tests for contribution ranking rules."""

import threading
from dataclasses import replace
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
RECENT_PUSH = NOW - timedelta(days=1)
# Long enough to avoid the short-body penalty, with no repro or code hints.
DEFAULT_BODY = (
    "The parser should report a clear error when the value is missing. Today it "
    "fails silently, and users only notice later when the output is empty. This "
    "issue tracks making that failure visible to the person running the command."
)


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
    created_days_ago: int = 30,
    comments: int = 0,
    title: str | None = None,
    body: str = DEFAULT_BODY,
) -> Issue:
    """Build a representative issue with overridable eligibility fields."""

    return Issue(
        repository="owner/project",
        number=number,
        title=title if title is not None else f"Handle a missing value in parser {number}",
        url=f"https://github.com/owner/project/issues/{number}",
        labels=labels,
        assignees=assignees,
        comments=comments,
        created_at=NOW - timedelta(days=created_days_ago),
        updated_at=NOW - timedelta(days=updated_days_ago),
        is_pull_request=is_pull_request,
        body=body,
    )


def test_discover_ranks_eligible_unassigned_issues() -> None:
    """Prefer strongly labeled, fresh issues and return stable ordering."""

    repository = Repository(
        name="owner/project",
        url="https://github.com/owner/project",
        stars=10_000,
        archived=False,
        pushed_at=RECENT_PUSH,
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
    assert candidates[0].reasons == ("label: bug", "label: good first issue", "updated 1 day ago")


def test_discover_filters_non_actionable_work() -> None:
    """Exclude assigned items, PRs, weak labels, and issues with competing PRs."""

    repository = Repository(
        name="owner/project",
        url="https://github.com/owner/project",
        stars=5_000,
        archived=False,
        pushed_at=RECENT_PUSH,
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
            "owner/small": Repository(
                "owner/small", "https://example.test/small", 99, False, RECENT_PUSH
            ),
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

    healthy = Repository(
        "owner/healthy", "https://github.com/owner/healthy", 10_000, False, RECENT_PUSH
    )
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
            "owner/archived": Repository(
                "owner/archived", "https://x.test/a", 20_000, True, RECENT_PUSH
            ),
            "owner/small": Repository("owner/small", "https://x.test/s", 99, False, RECENT_PUSH),
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


PROJECT = Repository(
    "owner/project", "https://github.com/owner/project", 10_000, False, RECENT_PUSH
)


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
        "updated 1 day ago",
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
    assert configured.candidates[0].reasons == ("label: contributor/wanted", "updated 1 day ago")


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
            "owner/first": Repository(
                "owner/first", "https://x.test/1", 10_000, False, RECENT_PUSH
            ),
            "owner/second": Repository(
                "owner/second", "https://x.test/2", 10_000, False, RECENT_PUSH
            ),
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
            name: Repository(name, f"https://x.test/{name}", 10_000, False, RECENT_PUSH)
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


# --- First-timer scoring rules -------------------------------------------------


def score_of(issue: Issue, repository: Repository = PROJECT) -> float | None:
    """Return one issue's score, or None when a hard filter excludes it."""

    radar = ContributionRadar(FakeGateway(repositories={}))
    weights = radar._contribution_weights(RepositoryTarget(repository.name))
    scored = radar._evaluate(repository, issue, weights, [], NOW)
    return None if scored is None else scored.score


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"updated_days_ago": 181}, "not updated for over 180 days"),
        ({"comments": 16}, "more than 15 comments"),
        ({"labels": ("help wanted", "status/blocked")}, "not-ready label"),
        ({"labels": ("good first issue", "needs-triage")}, "not-ready label"),
        ({"labels": ("help wanted", "won't fix")}, "not-ready label"),
        ({"title": "Tracking: move the CLI to the new parser"}, "meta title"),
        ({"title": "[RFC] Pluggable output formats"}, "meta title"),
    ],
)
def test_hard_filters_exclude_issues_that_are_not_ready(
    change: dict[str, object], reason: str
) -> None:
    """Exclude stale, busy, blocked and meta issues outright."""

    assert score_of(make_issue(1, **change)) is None, reason  # type: ignore[arg-type]


def test_security_label_does_not_exclude_an_issue() -> None:
    """Keep security-labelled issues: many targets are security tools."""

    assert score_of(make_issue(1, labels=("help wanted", "security"))) is not None


def test_contribution_labels_do_not_add_up() -> None:
    """Score only the strongest contribution label."""

    both = score_of(make_issue(1, labels=("good first issue", "help wanted")))
    single = score_of(make_issue(1, labels=("good first issue",)))
    assert both == single


def test_beginner_hint_lifts_a_bare_help_wanted_label() -> None:
    """Treat help wanted plus an easy/beginner label as nearly a good first issue."""

    bare = score_of(make_issue(1, labels=("help wanted",)))
    hinted = score_of(make_issue(1, labels=("help wanted", "difficulty/beginner")))
    assert bare is not None and hinted is not None
    assert hinted - bare == 11.0


@pytest.mark.parametrize(
    ("updated_days_ago", "expected"),
    [(0, 30.0), (3, 28.6), (30, 18.9), (45, 15.0), (90, 7.5), (180, 1.9)],
)
def test_freshness_decays_with_a_half_life(updated_days_ago: int, expected: float) -> None:
    """Halve the freshness reward every 45 days of inactivity."""

    assert round(ContributionRadar._freshness_score(updated_days_ago), 1) == expected


def test_freshness_separates_issues_within_the_same_month() -> None:
    """Rank an issue touched this week above one touched weeks ago."""

    assert ContributionRadar._freshness_score(3) > ContributionRadar._freshness_score(25)


@pytest.mark.parametrize(
    ("comments", "expected"),
    [(0, 8.0), (1, 15.0), (4, 15.0), (5, 8.0), (8, 8.0), (9, 2.0), (15, 2.0)],
)
def test_discussion_sweet_spot(comments: int, expected: float) -> None:
    """Prefer a few comments over none, and none over a long thread."""

    assert ContributionRadar._discussion_score(comments) == expected


@pytest.mark.parametrize(
    ("pushed_days_ago", "expected"),
    [(None, 0.0), (14, 10.0), (15, 5.0), (60, 5.0), (61, 0.0)],
)
def test_vitality_follows_the_last_push(pushed_days_ago: int | None, expected: float) -> None:
    """Reward repositories that were pushed to recently."""

    pushed_at = None if pushed_days_ago is None else NOW - timedelta(days=pushed_days_ago)
    repository = Repository("owner/project", "https://x.test", 10_000, False, pushed_at)
    assert ContributionRadar._vitality_score(repository, NOW) == expected


@pytest.mark.parametrize(
    ("stars", "expected"),
    [(500, 0.0), (2_000, 0.0), (10_000, 8.2), (50_000, 16.5), (100_000, 20.0), (500_000, 20.0)],
)
def test_popularity_rewards_big_projects_on_a_log_scale(stars: int, expected: float) -> None:
    """Give 0 points at 2k stars, rising to 20 at 100k and above."""

    assert round(ContributionRadar._popularity_score(stars), 1) == expected


@pytest.mark.parametrize(
    ("labels", "delta"),
    [
        (("help wanted", "size/XS"), 10.0),
        (("help wanted", "effort: low"), 10.0),
        (("help wanted", "size/XL"), -15.0),
        (("help wanted", "kind/complex"), -15.0),
    ],
)
def test_size_labels_adjust_the_score(labels: tuple[str, ...], delta: float) -> None:
    """Trust explicit maintainer size labels as a difficulty signal."""

    base = score_of(make_issue(1))
    sized = score_of(make_issue(1, labels=labels))
    assert base is not None and sized is not None
    assert round(sized - base, 1) == delta


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Handle a missing value in parser", 0.0),
        ("Fix typo in the install guide", 5.0),
        ("Refactor the scheduler for speed", -5.0),
        ("Why does this fail?", -5.0),
        ("Crash on start", -5.0),
        ("Refactor typo handling in docs", 0.0),
    ],
)
def test_title_hints_are_small_and_clamped(title: str, expected: float) -> None:
    """Use title wording only as a weak hint about scope and clarity."""

    assert ContributionRadar._title_score(title) == expected


@pytest.mark.parametrize(
    ("created_days_ago", "comments", "expected"),
    [(3, 0, -3.0), (3, 2, 0.0), (30, 0, 0.0), (731, 0, -5.0)],
)
def test_age_penalizes_untriaged_and_very_old_issues(
    created_days_ago: int, comments: int, expected: float
) -> None:
    """Penalize issues too new to be triaged, or older than two years."""

    issue = make_issue(1, created_days_ago=created_days_ago, comments=comments)
    assert ContributionRadar._age_score(issue, NOW) == expected


def test_body_rewards_code_pointers_and_repro_steps() -> None:
    """Reward a body that shows where to start, and penalize a very short one."""

    detailed = DEFAULT_BODY + "\n\nSteps to reproduce: run `cli parse` on src/parser/config.py."
    assert ContributionRadar._body_score(detailed) == (10.0, ["points to code", "has repro steps"])
    assert ContributionRadar._body_score(DEFAULT_BODY) == (0.0, [])
    assert ContributionRadar._body_score("") == (-5.0, [])


@pytest.mark.parametrize(
    ("body", "competing"),
    [
        ("Refs #4", True),
        ("Part of owner/project#4", True),
        ("See https://github.com/owner/project/issues/4 for context", True),
        ("Fixes 4", True),
        ("Refs #42", False),
        ("Refs other/project#4", False),
    ],
)
def test_any_reference_from_an_open_pull_request_counts_as_competing(
    body: str, competing: bool
) -> None:
    """Treat any open PR that mentions the issue as work already underway."""

    pull_requests = [PullRequest(title="Implement requested behavior", body=body)]
    issue = make_issue(4)
    assert (
        ContributionRadar._has_competing_pull_request("owner/project", issue, pull_requests)
        is competing
    )


@pytest.mark.parametrize(("flag", "detail"), [("disabled", "disabled"), ("fork", "fork")])
def test_scan_skips_disabled_and_forked_repositories(flag: str, detail: str) -> None:
    """Skip repositories that cannot accept contributions upstream."""

    repository = Repository(
        "owner/project", "https://x.test", 10_000, False, RECENT_PUSH, **{flag: True}
    )
    gateway = FakeGateway(
        repositories={"owner/project": repository}, issues={"owner/project": [make_issue(1)]}
    )
    result = ContributionRadar(gateway).scan([RepositoryTarget("owner/project")], now=NOW)
    assert [(scan.status, scan.detail) for scan in result.repositories] == [
        (ScanStatus.SKIPPED, detail)
    ]


def test_ranking_caps_each_repository_at_four() -> None:
    """Keep one repository from filling the list."""

    gateway = FakeGateway(
        repositories={"owner/project": PROJECT},
        issues={"owner/project": [make_issue(number) for number in range(1, 8)]},
    )
    result = ContributionRadar(gateway).scan([RepositoryTarget("owner/project")], now=NOW)
    assert [candidate.issue.number for candidate in result.candidates] == [1, 2, 3, 4]


def test_ranking_caps_docs_only_picks_at_three() -> None:
    """Allow at most three documentation-only picks; docs bugs are not capped."""

    names = [f"owner/repo{index}" for index in range(5)]
    repositories = {
        name: Repository(name, f"https://x.test/{name}", 10_000, False, RECENT_PUSH)
        for name in names
    }
    issues = {
        name: [
            replace(make_issue(1, labels=("help wanted", "documentation")), repository=name),
        ]
        for name in names
    }
    issues["owner/repo4"].append(
        replace(make_issue(2, labels=("help wanted", "documentation", "bug")), repository=names[4])
    )
    gateway = FakeGateway(repositories=repositories, issues=issues)
    result = ContributionRadar(gateway).scan([RepositoryTarget(name) for name in names], now=NOW)

    docs_only = [c for c in result.candidates if "label: bug" not in c.reasons]
    assert len(docs_only) == 3
    assert any("label: bug" in c.reasons for c in result.candidates)


def test_ranking_drops_candidates_below_the_minimum_score() -> None:
    """Publish fewer picks rather than pad the list with weak ones."""

    weak = make_issue(
        1,
        title="Crash?",
        body="",
        updated_days_ago=170,
        comments=12,
        labels=("help wanted", "size/XL"),
    )
    small = Repository("owner/project", "https://x.test", 2_000, False)
    gateway = FakeGateway(repositories={"owner/project": small}, issues={"owner/project": [weak]})
    result = ContributionRadar(gateway).scan([RepositoryTarget("owner/project")], now=NOW)
    assert result.candidates == ()
    assert result.repositories[0].eligible == 1


def test_ties_prefer_the_more_recently_updated_issue() -> None:
    """Break score ties by recency, then by fewer comments."""

    gateway = FakeGateway(
        repositories={"owner/project": PROJECT},
        issues={
            "owner/project": [
                make_issue(1, updated_days_ago=20),
                make_issue(2, updated_days_ago=5),
            ]
        },
    )
    result = ContributionRadar(gateway).scan([RepositoryTarget("owner/project")], now=NOW)
    assert [candidate.issue.number for candidate in result.candidates] == [2, 1]
