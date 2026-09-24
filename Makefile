# Stable developer interface; CI runs ./localPipeline.sh which uses the same commands.
.PHONY: install run demo test e2e lint format typecheck migrate refresh doctor build docker-build docker-run pipeline clean

IMAGE ?= gitlab-team-pulse:local

install:  ## create .venv with all dev dependencies
	uv sync --locked

run:  ## serve the dashboard (needs TEAMPULSE_GITLAB_URL and a token)
	uv run gitlab-team-pulse serve

demo:  ## serve the dashboard against the built-in fake GitLab
	uv run gitlab-team-pulse demo

test:  ## unit + integration tests with the 95% coverage gate
	uv run pytest --cov --cov-report=term-missing --cov-fail-under=95

e2e:  ## browser end-to-end tests (Playwright/Chromium)
	uv run playwright install chromium
	uv run pytest -m e2e

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --fix .
	uv run ruff format .

typecheck:
	uv run mypy

migrate:
	uv run gitlab-team-pulse migrate

refresh:
	uv run gitlab-team-pulse refresh

doctor:
	uv run gitlab-team-pulse doctor

build:
	rm -rf dist && uv build

docker-build:
	docker build -t $(IMAGE) .

docker-run: docker-build  ## demo container with a persistent volume on http://localhost:8000
	docker run --rm -p 8000:8000 -v teampulse-data:/data $(IMAGE) demo --host 0.0.0.0

pipeline:
	./localPipeline.sh

clean:
	rm -rf dist build htmlcov .coverage coverage.xml .pytest_cache .ruff_cache .mypy_cache
