# syntax=docker/dockerfile:1.7
# GitLab Team Pulse: one Python process, one SQLite database on a persistent volume.

FROM ghcr.io/astral-sh/uv:0.12.18 AS uv

FROM python:3.14-slim AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM python:3.14-slim AS runtime
ARG VERSION=dev
LABEL org.opencontainers.image.title="GitLab Team Pulse" \
      org.opencontainers.image.description="People-centric activity dashboard for self-managed GitLab" \
      org.opencontainers.image.source="https://github.com/marcelpetrick/GitLabTeamPulse" \
      org.opencontainers.image.licenses="GPL-3.0-or-later" \
      org.opencontainers.image.version="${VERSION}"
RUN groupadd --system --gid 10001 teampulse \
    && useradd --system --uid 10001 --gid teampulse --home-dir /app --shell /usr/sbin/nologin teampulse \
    && mkdir -p /data && chown teampulse:teampulse /data && chmod 0700 /data
COPY --from=build --chown=teampulse:teampulse /app/.venv /app/.venv
COPY --chmod=0755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    TEAMPULSE_DATABASE_PATH=/data/teampulse.db \
    TEAMPULSE_HOST=0.0.0.0 \
    TEAMPULSE_PORT=8000
# The GitLab token is read from the Docker secret /run/secrets/gitlab_token
# (or TEAMPULSE_GITLAB_TOKEN_FILE / TEAMPULSE_GITLAB_TOKEN). It is never baked into the image.
USER teampulse
WORKDIR /app
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"TEAMPULSE_PORT\", \"8000\")}/api/health', timeout=4)"
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["serve"]
