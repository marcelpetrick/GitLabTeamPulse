"""Bump the package version: every commit bumps patch, major features bump minor.

Usage: python tools/bump_version.py [patch|minor|major]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

VERSION_FILE = Path(__file__).resolve().parents[1] / "src" / "gitlab_team_pulse" / "__init__.py"
PATTERN = re.compile(r'__version__ = "(\d+)\.(\d+)\.(\d+)"')


def bump(text: str, part: str) -> tuple[str, str]:
    match = PATTERN.search(text)
    if match is None:
        raise SystemExit("no __version__ found")
    major, minor, patch = (int(value) for value in match.groups())
    if part == "major":
        major, minor, patch = major + 1, 0, 0
    elif part == "minor":
        minor, patch = minor + 1, 0
    elif part == "patch":
        patch += 1
    else:
        raise SystemExit(f"unknown part: {part}")
    version = f"{major}.{minor}.{patch}"
    return PATTERN.sub(f'__version__ = "{version}"', text, count=1), version


def main() -> None:
    part = sys.argv[1] if len(sys.argv) > 1 else "patch"
    new_text, version = bump(VERSION_FILE.read_text(encoding="utf-8"), part)
    VERSION_FILE.write_text(new_text, encoding="utf-8")
    print(version)


if __name__ == "__main__":
    main()
