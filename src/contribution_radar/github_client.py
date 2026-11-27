"""Read-only GitHub API adapter."""

from __future__ import annotations

import gzip
import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime
from typing import Any, cast

from contribution_radar.models import Issue, PullRequest, Repository
from contribution_radar.ports import RepositoryGatewayError

REPOSITORY_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
GITHUB_API_URL = "https://api.github.com"
PAGE_SIZE = 100
MAX_LABEL_PAGES = 10
# Retries per request, shared by rate limits and dropped connections.
MAX_RETRIES = 2
TRANSIENT_RETRY_BASE_SECONDS = 2.0
MAX_RATE_LIMIT_WAIT_SECONDS = 120.0
# GitHub's guidance when a rate-limited response gives no wait time.
DEFAULT_RATE_LIMIT_WAIT_SECONDS = 60.0


class GitHubApiError(RepositoryGatewayError):
    """Raised when GitHub cannot provide valid repository data."""


class GitHubClient:
    """Fetch public repository data using GitHub's REST API."""

    def __init__(
        self,
        token: str | None = None,
        timeout_seconds: float = 20.0,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Initialize the adapter with an optional API token.

        ``sleep`` and ``clock`` are injectable so rate-limit retries can be tested
        without waiting.
        """

        self._token = token
        self._timeout_seconds = timeout_seconds
        self._sleep = sleep
        self._clock = clock

    def get_repository(self, name: str) -> Repository:
        """Return popularity and lifecycle metadata for a repository."""

        payload = self._request_object(f"/repos/{self._validate_repository_name(name)}")
        return Repository(
            name=self._required_string(payload, "full_name"),
            url=self._required_string(payload, "html_url"),
            stars=self._required_int(payload, "stargazers_count"),
            archived=bool(payload.get("archived", False)),
            pushed_at=self._optional_datetime(payload, "pushed_at"),
            fork=bool(payload.get("fork", False)),
            disabled=bool(payload.get("disabled", False)),
        )

    def list_labels(self, name: str) -> list[str]:
        """Return every label name defined in a repository, up to 1,000."""

        repository = self._validate_repository_name(name)
        names: list[str] = []
        for page in range(1, MAX_LABEL_PAGES + 1):
            payload = self._request_list(
                f"/repos/{repository}/labels?per_page={PAGE_SIZE}&page={page}"
            )
            names.extend(self._required_string(item, "name") for item in payload)
            if len(payload) < PAGE_SIZE:
                break
        return names

    def list_open_issues(self, name: str, label: str) -> list[Issue]:
        """Return up to 100 recently updated open issues and pull requests with a label."""

        repository = self._validate_repository_name(name)
        encoded_label = urllib.parse.quote(label, safe="")
        payload = self._request_list(
            f"/repos/{repository}/issues?state=open&labels={encoded_label}"
            f"&sort=updated&direction=desc&per_page={PAGE_SIZE}"
        )
        return [self._parse_issue(repository, item) for item in payload]

    def list_open_pull_requests(self, name: str) -> list[PullRequest]:
        """Return up to 100 open pull requests for duplicate-work detection."""

        repository = self._validate_repository_name(name)
        payload = self._request_list(f"/repos/{repository}/pulls?state=open&per_page={PAGE_SIZE}")
        return [
            PullRequest(
                title=self._required_string(item, "title"),
                body=str(item.get("body") or ""),
            )
            for item in payload
        ]

    def _request_object(self, path: str) -> dict[str, Any]:
        """Request one JSON object from GitHub."""

        payload = self._request(path)
        if not isinstance(payload, dict):
            raise GitHubApiError(f"GitHub returned an unexpected object for {path}")
        return cast(dict[str, Any], payload)

    def _request_list(self, path: str) -> list[dict[str, Any]]:
        """Request a JSON list of objects from GitHub."""

        payload = self._request(path)
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise GitHubApiError(f"GitHub returned an unexpected list for {path}")
        return cast(list[dict[str, Any]], payload)

    def _request(self, path: str) -> object:
        """Perform one authenticated, read-only API request."""

        request = urllib.request.Request(
            f"{GITHUB_API_URL}{path}",
            headers={
                "Accept": "application/vnd.github+json",
                # Large issue pages are several hundred KB uncompressed.
                "Accept-Encoding": "gzip",
                "User-Agent": "oss-contribution-radar",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            method="GET",
        )
        if self._token:
            request.add_header("Authorization", f"Bearer {self._token}")

        attempt = 0
        while True:
            try:
                with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                    body = response.read()
                    if response.headers.get("Content-Encoding") == "gzip":
                        body = gzip.decompress(body)
                    return cast(object, json.loads(body))
            except urllib.error.HTTPError as error:
                wait = self._rate_limit_wait(error)
                if wait is None or attempt == MAX_RETRIES:
                    raise GitHubApiError(f"GitHub request failed for {path}: {error!r}") from error
            # OSError covers URL errors, timeouts and connections reset mid-read;
            # ValueError covers a truncated, non-JSON or corrupt gzip body.
            except (OSError, http.client.HTTPException, ValueError) as error:
                if not self._is_transient(error) or attempt == MAX_RETRIES:
                    raise GitHubApiError(f"GitHub request failed for {path}: {error!r}") from error
                wait = TRANSIENT_RETRY_BASE_SECONDS * 2**attempt
            attempt += 1
            self._sleep(wait)

    @staticmethod
    def _is_transient(error: Exception) -> bool:
        """Return whether a failure is a dropped or stalled connection worth retrying.

        Large responses (an open pull request page can exceed 2 MB, and GitHub does
        not compress it) are the most likely to be cut off mid-transfer.
        """

        if isinstance(error, urllib.error.URLError):
            return isinstance(error.reason, (ConnectionError, TimeoutError))
        return isinstance(error, (ConnectionError, TimeoutError, http.client.IncompleteRead))

    def _rate_limit_wait(self, error: urllib.error.HTTPError) -> float | None:
        """Return how long to wait before retrying a rate-limited response.

        Follows GitHub's guidance: honor ``Retry-After``, else wait for the reset
        time when the quota is used up, else wait a minute. Returns ``None`` when
        the response is not a rate limit or the wait would be too long to be useful.
        """

        if error.code not in (403, 429):
            return None
        headers = error.headers
        retry_after = headers.get("Retry-After") if headers else None
        remaining = headers.get("X-RateLimit-Remaining") if headers else None
        reset = headers.get("X-RateLimit-Reset") if headers else None

        if retry_after is not None and retry_after.isdigit():
            wait = float(retry_after)
        elif remaining == "0" and reset is not None and reset.isdigit():
            wait = max(float(reset) - self._clock(), 0.0) + 1.0
        elif error.code == 429 or self._mentions_rate_limit(error):
            wait = DEFAULT_RATE_LIMIT_WAIT_SECONDS
        else:
            return None
        return wait if wait <= MAX_RATE_LIMIT_WAIT_SECONDS else None

    @staticmethod
    def _mentions_rate_limit(error: urllib.error.HTTPError) -> bool:
        """Tell a rate-limit 403 apart from a permissions 403 by its message."""

        try:
            return b"rate limit" in error.read().lower()
        except (OSError, http.client.HTTPException):
            return False

    @staticmethod
    def _validate_repository_name(name: str) -> str:
        """Reject malformed names before interpolating them into an API path."""

        if not REPOSITORY_NAME.fullmatch(name):
            raise ValueError(f"invalid repository name: {name!r}")
        return name

    @staticmethod
    def _required_string(payload: dict[str, Any], key: str) -> str:
        """Extract a required string field from a GitHub response."""

        value = payload.get(key)
        if not isinstance(value, str) or not value:
            raise GitHubApiError(f"GitHub response is missing string field {key!r}")
        return value

    @staticmethod
    def _required_int(payload: dict[str, Any], key: str) -> int:
        """Extract a required integer field from a GitHub response."""

        value = payload.get(key)
        if not isinstance(value, int):
            raise GitHubApiError(f"GitHub response is missing integer field {key!r}")
        return value

    @classmethod
    def _parse_issue(cls, repository: str, payload: dict[str, Any]) -> Issue:
        """Normalize one item from GitHub's shared issues endpoint."""

        labels = tuple(
            str(label["name"])
            for label in payload.get("labels", [])
            if isinstance(label, dict) and isinstance(label.get("name"), str)
        )
        assignees = tuple(
            str(assignee["login"])
            for assignee in payload.get("assignees", [])
            if isinstance(assignee, dict) and isinstance(assignee.get("login"), str)
        )
        return Issue(
            repository=repository,
            number=cls._required_int(payload, "number"),
            title=cls._required_string(payload, "title"),
            url=cls._required_string(payload, "html_url"),
            labels=labels,
            assignees=assignees,
            comments=cls._required_int(payload, "comments"),
            created_at=cls._parse_datetime(cls._required_string(payload, "created_at")),
            updated_at=cls._parse_datetime(cls._required_string(payload, "updated_at")),
            is_pull_request="pull_request" in payload,
            body=str(payload.get("body") or ""),
        )

    @classmethod
    def _optional_datetime(cls, payload: dict[str, Any], key: str) -> datetime | None:
        """Parse a timestamp field that GitHub may omit or return as null."""

        value = payload.get(key)
        return cls._parse_datetime(value) if isinstance(value, str) else None

    @staticmethod
    def _parse_datetime(value: str) -> datetime:
        """Parse GitHub's UTC ISO-8601 timestamps."""

        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise GitHubApiError(f"invalid GitHub timestamp: {value!r}") from error
