"""Documentation stays navigable: relative links resolve and the architecture has diagrams."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOP_LEVEL = ("README.md", "CHANGELOG.md", "SECURITY.md")
DOCS = [*(ROOT / name for name in TOP_LEVEL), *sorted((ROOT / "docs").glob("*.md"))]
LINK = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")


@pytest.mark.parametrize("document", DOCS, ids=lambda p: p.name)
def test_relative_links_resolve(document: Path) -> None:
    broken = [
        target
        for target in LINK.findall(document.read_text(encoding="utf-8"))
        if not target.startswith(("http://", "https://", "mailto:"))
        and not (document.parent / target).exists()
    ]
    assert broken == []


def test_architecture_covers_the_c4_levels() -> None:
    text = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    for heading in ("Level 1: System context", "Level 2: Containers", "Level 3: Components"):
        assert f"## {heading}" in text
    assert text.count("```mermaid") >= 6
    assert "docs/ARCHITECTURE.md" in (ROOT / "README.md").read_text(encoding="utf-8")


def test_every_setting_is_in_the_readme_configuration_table() -> None:
    """A new ``TEAMPULSE_*`` setting must be documented (combined rows use ``/ _SUFFIX``)."""
    from gitlab_team_pulse.config import Settings

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    rows = [line for line in readme.splitlines() if line.startswith("| `TEAMPULSE_")]
    names: set[str] = set()
    for row in rows:
        first = row.split("|")[1]
        prefix = ""
        for token in re.findall(r"`(TEAMPULSE_[A-Z_]+|_[A-Z_]+)`", first):
            if token.startswith("TEAMPULSE_"):
                names.add(token)
                prefix = token
            else:  # ``_MAX_RETRIES`` after ``TEAMPULSE_GITLAB_TIMEOUT_SECONDS``
                names.add("_".join(prefix.split("_")[:2]) + token)
    missing = {f"TEAMPULSE_{f.upper()}" for f in Settings.model_fields} - names
    assert not missing, f"undocumented settings: {sorted(missing)}"
