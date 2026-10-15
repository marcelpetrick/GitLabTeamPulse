"""Facts for the About dialog, read from the installed package metadata (never hard-coded)."""

from __future__ import annotations

import platform
import re
from functools import cache
from importlib import metadata
from typing import Any

from gitlab_team_pulse import __version__

DISTRIBUTION = "gitlab-team-pulse"
REPOSITORY = "https://github.com/marcelpetrick/GitLabTeamPulse"
REQUIREMENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")


def _license(meta: metadata.PackageMetadata) -> str:
    """SPDX expression when declared, else the License field, else the trove classifier."""
    expression = meta.get("License-Expression")
    if expression:
        return str(expression)
    text = (meta.get("License") or "").strip()
    if text and "\n" not in text and len(text) <= 60:
        return text
    for classifier in meta.get_all("Classifier") or []:
        if classifier.startswith("License :: "):
            return str(classifier.rsplit(" :: ", 1)[-1])
    return "unknown"


@cache
def about() -> dict[str, Any]:
    """Name, version, author, license and the runtime dependencies with versions and licenses."""
    try:
        meta = metadata.metadata(DISTRIBUTION)
        requires = metadata.requires(DISTRIBUTION) or []
    except metadata.PackageNotFoundError:  # pragma: no cover - always installed in practice
        meta, requires = None, []
    dependencies: list[dict[str, str | None]] = []
    for requirement in requires:
        if ";" in requirement and "extra" in requirement.split(";", 1)[1]:
            continue  # optional extras are not part of the running application
        match = REQUIREMENT_NAME.match(requirement)
        if match is None:  # pragma: no cover - malformed metadata
            continue
        name = match.group(0)
        try:
            dep = metadata.metadata(name)
            dependencies.append(
                {
                    "name": dep.get("Name") or name,
                    "version": metadata.version(name),
                    "license": _license(dep),
                }
            )
        except metadata.PackageNotFoundError:
            dependencies.append({"name": name, "version": None, "license": "unknown"})
    dependencies.sort(key=lambda d: str(d["name"]).lower())
    return {
        "name": "GitLab Team Pulse",
        "version": __version__,
        "description": (meta.get("Summary") if meta else None)
        or "People-centric activity dashboard for a self-managed GitLab instance.",
        "author": "Marcel Petrick",
        "email": "mail@marcelpetrick.it",
        "license": _license(meta) if meta else "GPL-3.0-or-later",
        "repository": REPOSITORY,
        "python": platform.python_version(),
        "dependencies": dependencies,
        "note": "This project is generated with AI.",
    }
