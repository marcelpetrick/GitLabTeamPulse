"""GitLab integration layer: the only code that talks HTTP to GitLab or reads raw payloads."""

from gitlab_team_pulse.gitlab.client import GitLabClient
from gitlab_team_pulse.gitlab.errors import GitLabError

__all__ = ["GitLabClient", "GitLabError"]
