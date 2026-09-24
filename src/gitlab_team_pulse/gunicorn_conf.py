"""Gunicorn settings for container/server deployments.

Exactly one worker: the in-process scheduler must have a single owner per SQLite database.
Usage: ``gunicorn -c python:gitlab_team_pulse.gunicorn_conf "gitlab_team_pulse.app:create_app()"``
"""

from __future__ import annotations

import os

bind = f"{os.environ.get('TEAMPULSE_HOST', '0.0.0.0')}:{os.environ.get('TEAMPULSE_PORT', '8000')}"  # noqa: S104
workers = 1
worker_class = "uvicorn_worker.UvicornWorker"
timeout = 60
graceful_timeout = 20
keepalive = 5
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("TEAMPULSE_LOG_LEVEL", "info").lower()
forwarded_allow_ips = os.environ.get("TEAMPULSE_FORWARDED_ALLOW_IPS", "127.0.0.1")
