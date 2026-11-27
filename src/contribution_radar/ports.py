"""Ports that separate discovery logic from external data providers."""

from typing import Protocol

from contribution_radar.models import Issue, PullRequest, Repository


class RepositoryGatewayError(RuntimeError):
    """Raised by a gateway when one repository's data cannot be retrieved."""


class RepositoryGateway(Protocol):
    """Interface required by the contribution-ranking service."""

    def get_repository(self, name: str) -> Repository:
        """Return metadata for one public repository."""
        ...

    def list_labels(self, name: str) -> list[str]:
        """Return the names of every label defined in a repository."""
        ...

    def list_open_issues(self, name: str, label: str) -> list[Issue]:
        """Return recently updated open issues and pull requests carrying one label."""
        ...

    def list_open_pull_requests(self, name: str) -> list[PullRequest]:
        """Return open pull requests used for duplicate-work detection."""
        ...
