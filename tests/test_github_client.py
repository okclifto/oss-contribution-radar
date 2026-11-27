"""Unit tests for the read-only GitHub adapter."""

import email.message
import gzip
import http.client
import io
import json
import urllib.error
import urllib.request

import pytest

from contribution_radar.github_client import GitHubApiError, GitHubClient


def test_client_normalizes_repository_issues_and_pull_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Convert representative GitHub payloads into typed domain models."""

    responses: dict[str, object] = {
        "/repos/owner/project": {
            "full_name": "owner/project",
            "html_url": "https://github.com/owner/project",
            "stargazers_count": 5_000,
            "archived": False,
        },
        (
            "/repos/owner/project/issues?state=open&labels=help%20wanted"
            "&sort=updated&direction=desc&per_page=100"
        ): [
            {
                "number": 7,
                "title": "Fix a bug",
                "html_url": "https://github.com/owner/project/issues/7",
                "labels": [{"name": "help wanted"}, {"invalid": True}],
                "assignees": [{"login": "alice"}, {"invalid": True}],
                "comments": 2,
                "created_at": "2026-09-01T00:00:00Z",
                "updated_at": "2026-09-16T00:00:00Z",
                "pull_request": {"url": "https://api.github.com/example"},
            }
        ],
        "/repos/owner/project/pulls?state=open&per_page=100": [
            {"title": "Existing work", "body": None}
        ],
    }
    client = GitHubClient()
    monkeypatch.setattr(client, "_request", lambda path: responses[path])

    repository = client.get_repository("owner/project")
    issues = client.list_open_issues("owner/project", "help wanted")
    pull_requests = client.list_open_pull_requests("owner/project")

    assert repository.stars == 5_000
    assert issues[0].labels == ("help wanted",)
    assert issues[0].assignees == ("alice",)
    assert issues[0].is_pull_request is True
    assert pull_requests[0].body == ""
    assert repository.pushed_at is None
    assert (repository.fork, repository.disabled) == (False, False)
    assert issues[0].body == ""


def test_client_reads_activity_and_issue_bodies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Parse the push time, fork and disabled flags, and issue bodies when present."""

    responses: dict[str, object] = {
        "/repos/owner/project": {
            "full_name": "owner/project",
            "html_url": "https://github.com/owner/project",
            "stargazers_count": 5_000,
            "pushed_at": "2026-09-15T12:00:00Z",
            "fork": True,
            "disabled": True,
        },
        (
            "/repos/owner/project/issues?state=open&labels=help%20wanted"
            "&sort=updated&direction=desc&per_page=100"
        ): [
            {
                "number": 7,
                "title": "Fix a bug",
                "html_url": "https://github.com/owner/project/issues/7",
                "comments": 0,
                "created_at": "2026-09-01T00:00:00Z",
                "updated_at": "2026-09-16T00:00:00Z",
                "body": "Steps to reproduce: run the CLI.",
            },
            {
                "number": 8,
                "title": "Fix another bug",
                "html_url": "https://github.com/owner/project/issues/8",
                "comments": 0,
                "created_at": "2026-09-01T00:00:00Z",
                "updated_at": "2026-09-16T00:00:00Z",
                "body": None,
            },
        ],
    }
    client = GitHubClient()
    monkeypatch.setattr(client, "_request", lambda path: responses[path])

    repository = client.get_repository("owner/project")
    issues = client.list_open_issues("owner/project", "help wanted")

    assert repository.pushed_at is not None
    assert repository.pushed_at.isoformat() == "2026-09-15T12:00:00+00:00"
    assert (repository.fork, repository.disabled) == (True, True)
    assert [issue.body for issue in issues] == ["Steps to reproduce: run the CLI.", ""]


def test_list_labels_follows_pages_until_a_short_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collect labels across full pages and stop at the first partial page."""

    requested: list[str] = []

    def fake_request(path: str) -> object:
        """Return two full pages of labels, then a final page of one."""

        requested.append(path)
        page = int(path.rsplit("=", 1)[1])
        size = 100 if page < 3 else 1
        return [{"name": f"label-{page}-{index}"} for index in range(size)]

    client = GitHubClient()
    monkeypatch.setattr(client, "_request", fake_request)

    labels = client.list_labels("owner/project")

    assert len(labels) == 201
    assert requested[-1] == "/repos/owner/project/labels?per_page=100&page=3"


def test_list_open_issues_encodes_label_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """Percent-encode slashes, spaces and emoji so the label filter stays intact."""

    requested: list[str] = []
    client = GitHubClient()
    monkeypatch.setattr(client, "_request", lambda path: requested.append(path) or [])

    client.list_open_issues("owner/project", "kind/help wanted 🆘")

    assert "labels=kind%2Fhelp%20wanted%20%F0%9F%86%98&" in requested[0]


@pytest.mark.parametrize("name", ["owner", "../owner/repo", "owner/repo/name", "owner/repo?x=1"])
def test_client_rejects_invalid_repository_names(name: str) -> None:
    """Prevent malformed repository names from entering request paths."""

    with pytest.raises(ValueError, match="invalid repository name"):
        GitHubClient().get_repository(name)


class FakeResponse(io.BytesIO):
    """An HTTP response body with headers, as urlopen returns."""

    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        """Store the body and response headers."""

        super().__init__(body)
        self.headers = email.message.Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value


def test_request_adds_token_and_decodes_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """Send authenticated requests with the expected API headers."""

    captured_request: urllib.request.Request | None = None

    def fake_urlopen(request: urllib.request.Request, timeout: float) -> FakeResponse:
        """Capture the request and return a JSON response stream."""

        nonlocal captured_request
        captured_request = request
        assert timeout == 3.0
        return FakeResponse(json.dumps({"ok": True}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    payload = GitHubClient(token="test-token", timeout_seconds=3.0)._request("/example")

    assert payload == {"ok": True}
    assert captured_request is not None
    assert captured_request.get_header("Authorization") == "Bearer test-token"
    assert captured_request.get_header("Accept-encoding") == "gzip"
    assert captured_request.get_method() == "GET"


def test_request_wraps_network_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Expose network failures through the adapter's stable exception."""

    def fail_urlopen(request: urllib.request.Request, timeout: float) -> object:
        """Simulate a GitHub HTTP failure."""

        del request, timeout
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)

    with pytest.raises(GitHubApiError, match="GitHub request failed"):
        GitHubClient()._request("/example")


def test_request_decompresses_gzip_bodies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Decode a gzip-compressed JSON body."""

    body = gzip.compress(json.dumps([{"name": "help wanted"}]).encode())
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda request, timeout: FakeResponse(body, {"Content-Encoding": "gzip"}),
    )

    assert GitHubClient()._request("/example") == [{"name": "help wanted"}]


class ResetResponse(FakeResponse):
    """A response whose body read fails as a dropped connection does."""

    def read(self, size: int | None = -1) -> bytes:
        """Fail partway through the body."""

        raise ConnectionResetError(54, "Connection reset by peer")


@pytest.mark.parametrize(
    "body",
    [
        ResetResponse(b""),
        FakeResponse(b"{not json"),
        FakeResponse(b"not gzip", {"Content-Encoding": "gzip"}),
    ],
)
def test_request_wraps_errors_while_reading_the_body(
    monkeypatch: pytest.MonkeyPatch, body: FakeResponse
) -> None:
    """Treat a reset connection or a truncated body as a per-repository failure."""

    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout: body)

    with pytest.raises(GitHubApiError, match="GitHub request failed"):
        GitHubClient(sleep=lambda seconds: None)._request("/example")


def test_client_rejects_malformed_api_payloads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject response shapes and required fields that violate the API contract."""

    client = GitHubClient()
    monkeypatch.setattr(client, "_request", lambda path: [] if path == "/object" else {})

    with pytest.raises(GitHubApiError, match="unexpected object"):
        client._request_object("/object")
    with pytest.raises(GitHubApiError, match="unexpected list"):
        client._request_list("/list")
    with pytest.raises(GitHubApiError, match="missing string"):
        client._required_string({}, "name")
    with pytest.raises(GitHubApiError, match="missing integer"):
        client._required_int({}, "count")
    with pytest.raises(GitHubApiError, match="invalid GitHub timestamp"):
        client._parse_datetime("not-a-date")


def http_error(code: int, headers: dict[str, str] | None = None, body: bytes = b"") -> Exception:
    """Build an HTTPError like urlopen raises for a non-2xx response."""

    message = email.message.Message()
    for key, value in (headers or {}).items():
        message[key] = value
    return urllib.error.HTTPError(
        "https://api.github.com/x", code, "error", message, io.BytesIO(body)
    )


def scripted_client(
    monkeypatch: pytest.MonkeyPatch, outcomes: list[object]
) -> tuple[GitHubClient, list[float]]:
    """Return a client whose requests play back outcomes, recording its sleeps."""

    sleeps: list[float] = []

    def fake_urlopen(request: urllib.request.Request, timeout: float) -> object:
        """Raise the next scripted error, or return the next response."""

        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return GitHubClient(sleep=sleeps.append, clock=lambda: 1_000.0), sleeps


OK = b'{"ok": true}'


@pytest.mark.parametrize(
    "error,wait",
    [
        (http_error(429, {"Retry-After": "5"}), 5.0),
        (http_error(403, {"Retry-After": "7"}), 7.0),
        (http_error(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1030"}), 31.0),
        (http_error(403, body=b'{"message": "You have exceeded a secondary rate limit"}'), 60.0),
        (http_error(429), 60.0),
    ],
)
def test_request_waits_and_retries_rate_limits(
    monkeypatch: pytest.MonkeyPatch, error: Exception, wait: float
) -> None:
    """Wait as GitHub instructs, then retry the same request."""

    client, sleeps = scripted_client(monkeypatch, [error, FakeResponse(OK)])

    assert client._request("/example") == {"ok": True}
    assert sleeps == [wait]


@pytest.mark.parametrize(
    "error",
    [
        http_error(403, body=b'{"message": "Resource not accessible by integration"}'),
        urllib.error.HTTPError(
            "https://api.github.com/x", 403, "error", email.message.Message(), ResetResponse(b"")
        ),
        http_error(404),
        http_error(500, {"Retry-After": "5"}),
        http_error(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "4600"}),
    ],
)
def test_request_fails_fast_when_waiting_would_not_help(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """Do not wait on permission errors, other failures, or an hour-long reset."""

    client, sleeps = scripted_client(monkeypatch, [error])

    with pytest.raises(GitHubApiError, match="GitHub request failed"):
        client._request("/example")
    assert sleeps == []


def test_request_gives_up_after_the_retry_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop after two retries so one repository cannot stall the scan."""

    client, sleeps = scripted_client(
        monkeypatch, [http_error(429, {"Retry-After": "1"}) for _ in range(3)]
    )

    with pytest.raises(GitHubApiError, match="429"):
        client._request("/example")
    assert sleeps == [1.0, 1.0]


@pytest.mark.parametrize(
    "error",
    [
        ConnectionResetError(54, "Connection reset by peer"),
        http.client.IncompleteRead(b"partial", 100),
        TimeoutError("The read operation timed out"),
        urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")),
    ],
)
def test_request_retries_dropped_connections_with_backoff(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """Retry a connection that drops or stalls, waiting longer each time."""

    client, sleeps = scripted_client(monkeypatch, [error, error, FakeResponse(OK)])

    assert client._request("/example") == {"ok": True}
    assert sleeps == [2.0, 4.0]


@pytest.mark.parametrize(
    "error",
    [urllib.error.URLError("Name or service not known"), ValueError("bad JSON")],
)
def test_request_does_not_retry_permanent_failures(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """Fail at once on DNS errors and malformed bodies, which a retry won't fix."""

    client, sleeps = scripted_client(monkeypatch, [error])

    with pytest.raises(GitHubApiError):
        client._request("/example")
    assert sleeps == []


def test_request_gives_up_on_a_connection_that_keeps_dropping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Report the failure after the retry budget is spent."""

    client, sleeps = scripted_client(monkeypatch, [ConnectionResetError() for _ in range(3)])

    with pytest.raises(GitHubApiError, match="ConnectionResetError"):
        client._request("/example")
    assert sleeps == [2.0, 4.0]
