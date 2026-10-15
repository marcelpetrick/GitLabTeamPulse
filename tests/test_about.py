"""The About dialog's facts come from the installed package metadata."""

from __future__ import annotations

from email.message import Message
from typing import Any

import pytest
from fastapi.testclient import TestClient

from gitlab_team_pulse import __version__, about
from gitlab_team_pulse.app import create_app
from gitlab_team_pulse.config import Settings


def test_about_endpoint(settings: Settings) -> None:
    app = create_app(settings.model_copy(update={"scheduler_enabled": False}))
    with TestClient(app) as client:
        info = client.get("/api/about").json()
    assert info["version"] == __version__
    assert info["author"] == "Marcel Petrick"
    assert info["license"] == "GPL-3.0-or-later"
    assert info["repository"].startswith("https://github.com/")
    names = {d["name"].lower() for d in info["dependencies"]}
    assert {"fastapi", "sqlalchemy", "httpx", "pydantic"} <= names
    assert all(d["version"] for d in info["dependencies"])
    assert all(d["license"] != "unknown" for d in info["dependencies"])
    assert info["note"] == "This project is generated with AI."


def metadata(**fields: Any) -> Message:
    message = Message()
    for key, value in fields.items():
        for item in value if isinstance(value, list) else [value]:
            message[key.replace("_", "-")] = item
    return message


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"License_Expression": "MIT"}, "MIT"),
        ({"License": "BSD License"}, "BSD License"),
        (
            {
                "License": "a very\nlong text",
                "Classifier": ["License :: OSI Approved :: MIT License"],
            },
            "MIT License",
        ),
        ({"Classifier": ["Programming Language :: Python"]}, "unknown"),
    ],
)
def test_license_detection(fields: dict[str, Any], expected: str) -> None:
    assert about._license(metadata(**fields)) == expected  # type: ignore[arg-type]


def test_missing_dependency_is_listed_as_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    about.about.cache_clear()
    real = about.metadata.metadata

    def fake_requires(name: str) -> list[str]:
        return ["fastapi==1", "not-installed-package>=1", 'extra-only; extra == "dev"']

    def fake_metadata(name: str) -> Any:
        if name == "not-installed-package":
            raise about.metadata.PackageNotFoundError(name)
        return real(name)

    monkeypatch.setattr(about.metadata, "requires", fake_requires)
    monkeypatch.setattr(about.metadata, "metadata", fake_metadata)
    try:
        deps = about.about()["dependencies"]
    finally:
        about.about.cache_clear()
    assert [d["name"].lower() for d in deps] == ["fastapi", "not-installed-package"]
    assert deps[1] == {"name": "not-installed-package", "version": None, "license": "unknown"}
