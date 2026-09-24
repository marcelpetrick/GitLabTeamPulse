#!/bin/sh
# Container entrypoint: "serve" migrates explicitly, then runs Gunicorn with one Uvicorn worker.
# Any other argument is passed to the CLI (demo, doctor, refresh, migrate, fake-gitlab, ...).
set -eu

if [ "${1:-serve}" = "serve" ]; then
    gitlab-team-pulse migrate
    exec gunicorn -c python:gitlab_team_pulse.gunicorn_conf "gitlab_team_pulse.app:create_app()"
fi
exec gitlab-team-pulse "$@"
