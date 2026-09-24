"""Exceptions raised by the GitLab integration layer.

Messages are safe to persist and show in the UI: they never contain the token, only the
operation, HTTP status and a short explanation.
"""

from __future__ import annotations


class GitLabError(Exception):
    """Base class; ``kind`` is a stable machine-friendly category for diagnostics."""

    kind = "gitlab_error"
    retryable = False

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GitLabAuthError(GitLabError):
    """401: the token is missing, invalid, expired or revoked. Fails fast, never retried."""

    kind = "authentication"


class GitLabForbiddenError(GitLabError):
    """403: the token lacks the scope or permission for this resource."""

    kind = "authorization"


class GitLabNotFoundError(GitLabError):
    kind = "not_found"


class GitLabRateLimitError(GitLabError):
    kind = "rate_limited"
    retryable = True


class GitLabUnavailableError(GitLabError):
    """Network failure, timeout or 5xx after bounded retries."""

    kind = "unavailable"
    retryable = True


class GitLabResponseError(GitLabError):
    """The server answered, but the payload was not what the API contract promises."""

    kind = "malformed_response"


class GitLabCapabilityError(GitLabError):
    """The installation does not offer an optional feature (e.g. GraphQL timelogs)."""

    kind = "capability"
