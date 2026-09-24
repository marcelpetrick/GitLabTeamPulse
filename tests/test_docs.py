"""Documentation stays navigable: relative links resolve and the architecture has diagrams."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = [ROOT / "README.md", ROOT / "CHANGELOG.md", *sorted((ROOT / "docs").glob("*.md"))]
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
