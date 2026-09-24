from __future__ import annotations

import importlib

import pytest

from gitlab_team_pulse import gunicorn_conf


def test_single_uvicorn_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAMPULSE_PORT", "9000")
    monkeypatch.setenv("TEAMPULSE_LOG_LEVEL", "DEBUG")
    conf = importlib.reload(gunicorn_conf)
    assert conf.workers == 1
    assert conf.worker_class == "uvicorn_worker.UvicornWorker"
    assert conf.bind == "0.0.0.0:9000"
    assert conf.loglevel == "debug"
    importlib.import_module(conf.worker_class.rsplit(".", 1)[0])
