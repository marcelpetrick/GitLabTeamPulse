#!/usr/bin/env bash
# Local project pipeline. CI (GitHub Actions and GitLab CI) runs exactly this script.
set -u
set -o pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${ROOT_DIR}" || exit 1

RUN_APP=true
RUN_E2E=true
RUN_DOCKER=auto
OPEN_REPORTS=true
REPORT_DIR=""
PIPELINE_LOG_DIR=""
REMOVE_PIPELINE_LOG_DIR=true
COVERAGE_MIN=95
IMAGE_TAG="gitlab-team-pulse:pipeline"

declare -a SUMMARY_LINES=()
FAILED=0

print_usage() {
    cat <<EOF
Usage: ./localPipeline.sh [--noRun] [--noE2E] [--noDocker] [--docker] [--noOpen] [--report-dir PATH]

Stages:
   1. uv sync --locked (Python environment with dev dependencies)
   2. Ruff lint
   3. Ruff format check
   4. mypy (strict)
   5. Migration check: fresh SQLite database migrated through the CLI
   6. Unit + integration tests with coverage (gate: ${COVERAGE_MIN}%), JUnit XML, htmlcov/
   7. Browser end-to-end tests (Playwright/Chromium)         --noE2E to skip
   8. Build sdist and wheel
   9. Install the wheel into a clean venv; import, CLI and HTTP smoke test (demo mode)
  10. Docker image build and container smoke test            auto when docker is available
  11. Open the coverage report                                --noOpen (implied in CI)
  12. Launch the demo dashboard once                          --noRun to skip
  13. Print a stage-by-stage summary

--report-dir keeps logs, junit.xml, coverage.xml and summary.txt; otherwise they are temporary.
EOF
}

log() { printf '\033[1;34m[INFO]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[WARN]\033[0m %s\n' "$*" >&2; }
error() { printf '\033[1;31m[ERROR]\033[0m %s\n' "$*" >&2; }

mark() {
    SUMMARY_LINES+=("$(printf '%-16s : %-4s %s' "$1" "$2" "$3")")
    if [[ "$2" == "FAIL" ]]; then FAILED=1; fi
}

run_logged() {
    local log_path="$1"
    shift
    "$@" 2>&1 | tee "${log_path}"
    return "${PIPESTATUS[0]}"
}

free_port() {
    python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'
}

wait_for_http() {
    local url="$1"
    local attempts="${2:-60}"
    for _ in $(seq "${attempts}"); do
        if curl -fsS "${url}" >/dev/null 2>&1; then return 0; fi
        sleep 0.5
    done
    return 1
}

parse_arguments() {
    while [[ "$#" -gt 0 ]]; do
        case "$1" in
            --noRun) RUN_APP=false ;;
            --noE2E) RUN_E2E=false ;;
            --noDocker) RUN_DOCKER=false ;;
            --docker) RUN_DOCKER=true ;;
            --noOpen) OPEN_REPORTS=false ;;
            --report-dir)
                if [[ "$#" -lt 2 ]]; then error "--report-dir requires a path"; exit 2; fi
                REPORT_DIR="$2"
                shift
                ;;
            -h|--help) print_usage; exit 0 ;;
            *) error "Unknown argument: $1"; print_usage; exit 2 ;;
        esac
        shift
    done
    if [[ -n "${CI:-}" ]]; then
        OPEN_REPORTS=false
        RUN_APP=false
    fi
}

prepare_report_directory() {
    if [[ -n "${REPORT_DIR}" ]]; then
        PIPELINE_LOG_DIR="$(realpath -m "${REPORT_DIR}")"
        REMOVE_PIPELINE_LOG_DIR=false
    else
        PIPELINE_LOG_DIR="$(mktemp -d "${TMPDIR:-/tmp}/teampulse-pipeline-XXXXXX")"
    fi
    mkdir -p "${PIPELINE_LOG_DIR}"
    trap 'if [[ "${REMOVE_PIPELINE_LOG_DIR}" == true ]]; then rm -rf "${PIPELINE_LOG_DIR}"; fi' EXIT
}

stage_sync() {
    log "Syncing the uv environment."
    if ! command -v uv >/dev/null 2>&1; then
        mark "Environment" "FAIL" "uv is not installed (https://docs.astral.sh/uv/)"
        return 1
    fi
    if run_logged "${PIPELINE_LOG_DIR}/sync.log" uv sync --locked; then
        mark "Environment" "PASS" "$(uv run python --version 2>&1), uv $(uv --version | cut -d' ' -f2)"
        return 0
    fi
    mark "Environment" "FAIL" "uv sync --locked failed (is uv.lock current?)"
    return 1
}

stage_ruff() {
    log "Running Ruff lint."
    if run_logged "${PIPELINE_LOG_DIR}/ruff.log" uv run ruff check .; then
        mark "Ruff lint" "PASS" "0 violations"
    else
        mark "Ruff lint" "FAIL" "$(grep -E 'Found [0-9]+ error' "${PIPELINE_LOG_DIR}/ruff.log" | tail -n1)"
    fi
    log "Running Ruff format check."
    if run_logged "${PIPELINE_LOG_DIR}/ruff-format.log" uv run ruff format --check .; then
        mark "Ruff format" "PASS" "$(tail -n1 "${PIPELINE_LOG_DIR}/ruff-format.log")"
    else
        mark "Ruff format" "FAIL" "files need formatting (run: make format)"
    fi
}

stage_mypy() {
    log "Running mypy."
    if run_logged "${PIPELINE_LOG_DIR}/mypy.log" uv run mypy; then
        mark "mypy" "PASS" "$(tail -n1 "${PIPELINE_LOG_DIR}/mypy.log")"
    else
        mark "mypy" "FAIL" "$(tail -n1 "${PIPELINE_LOG_DIR}/mypy.log")"
    fi
}

stage_migrations() {
    log "Migrating a fresh SQLite database through the CLI."
    local db_dir
    db_dir="$(mktemp -d)"
    if run_logged "${PIPELINE_LOG_DIR}/migrate.log" \
        env TEAMPULSE_DATABASE_PATH="${db_dir}/fresh.db" uv run gitlab-team-pulse migrate; then
        mark "Migrations" "PASS" "$(tail -n1 "${PIPELINE_LOG_DIR}/migrate.log")"
    else
        mark "Migrations" "FAIL" "migration of an empty database failed"
    fi
    rm -rf "${db_dir}"
}

stage_tests() {
    log "Running unit and integration tests with coverage."
    local log_path="${PIPELINE_LOG_DIR}/pytest.log"
    if run_logged "${log_path}" uv run pytest -m "not e2e" \
        --cov --cov-report=term-missing --cov-report=html:htmlcov \
        --cov-report="xml:${PIPELINE_LOG_DIR}/coverage.xml" \
        --cov-fail-under="${COVERAGE_MIN}" \
        --junitxml="${PIPELINE_LOG_DIR}/junit.xml"; then
        local status="PASS"
    else
        local status="FAIL"
    fi
    local coverage result
    coverage="$(grep -Eo 'Total coverage: [0-9.]+%' "${log_path}" | tail -n1 | cut -d' ' -f3)"
    result="$(grep -E '^=+ .*(passed|failed).* =+$' "${log_path}" | tail -n1 | tr -d '=' | xargs)"
    mark "Tests+Coverage" "${status}" "${coverage:-n/a} coverage (gate ${COVERAGE_MIN}%); ${result}"
}

stage_e2e() {
    if [[ "${RUN_E2E}" == false ]]; then
        mark "E2E (browser)" "SKIP" "disabled by --noE2E"
        return
    fi
    log "Installing Playwright Chromium (cached after the first run)."
    local install_args=(chromium)
    if [[ -n "${CI:-}" ]]; then install_args+=(--with-deps); fi
    if ! run_logged "${PIPELINE_LOG_DIR}/playwright-install.log" uv run playwright install "${install_args[@]}"; then
        mark "E2E (browser)" "FAIL" "could not install Playwright Chromium"
        return
    fi
    log "Running browser end-to-end tests."
    local log_path="${PIPELINE_LOG_DIR}/e2e.log"
    if run_logged "${log_path}" uv run pytest -m e2e -p no:cacheprovider \
        --junitxml="${PIPELINE_LOG_DIR}/junit-e2e.xml"; then
        mark "E2E (browser)" "PASS" "$(grep -E '^=+ .*passed.* =+$' "${log_path}" | tail -n1 | tr -d '=' | xargs)"
    else
        mark "E2E (browser)" "FAIL" "see ${log_path}"
    fi
}

stage_build() {
    log "Building sdist and wheel."
    rm -rf "${ROOT_DIR}/dist"
    if run_logged "${PIPELINE_LOG_DIR}/build.log" uv build; then
        WHEEL="$(find "${ROOT_DIR}/dist" -maxdepth 1 -name 'gitlab_team_pulse-*.whl' -print -quit)"
        mark "Package build" "PASS" "$(basename "${WHEEL}") + sdist"
        return 0
    fi
    mark "Package build" "FAIL" "uv build failed"
    return 1
}

stage_wheel_smoke() {
    log "Installing the wheel into a clean virtual environment and smoke testing it."
    local venv work gitlab_port app_port pid version
    work="$(mktemp -d)"
    venv="${work}/venv"
    if ! uv venv --quiet "${venv}" >/dev/null 2>&1 \
        || ! uv pip install --quiet --python "${venv}/bin/python" "${WHEEL}" >"${PIPELINE_LOG_DIR}/wheel-install.log" 2>&1; then
        mark "Wheel smoke" "FAIL" "wheel installation failed"
        rm -rf "${work}"
        return
    fi
    version="$(cd "${work}" && "${venv}/bin/gitlab-team-pulse" --version)"
    gitlab_port="$(free_port)"
    app_port="$(free_port)"
    (cd "${work}" && exec "${venv}/bin/gitlab-team-pulse" demo --port "${app_port}" \
        --gitlab-port "${gitlab_port}" --database "${work}/demo.db") >"${PIPELINE_LOG_DIR}/wheel-smoke.log" 2>&1 &
    pid=$!
    if wait_for_http "http://127.0.0.1:${app_port}/api/health" \
        && curl -fsS "http://127.0.0.1:${app_port}/" | grep -q "GitLab Team Pulse" \
        && curl -fsS "http://127.0.0.1:${app_port}/static/app.js" >/dev/null \
        && curl -fsS "http://127.0.0.1:${app_port}/api/dashboard" | grep -q '"cards"'; then
        mark "Wheel smoke" "PASS" "${version}: CLI, static assets and API served from the wheel"
    else
        mark "Wheel smoke" "FAIL" "installed app did not serve correctly (see wheel-smoke.log)"
        tail -n 40 "${PIPELINE_LOG_DIR}/wheel-smoke.log" >&2
    fi
    kill "${pid}" >/dev/null 2>&1
    wait "${pid}" 2>/dev/null
    rm -rf "${work}"
}

stage_docker() {
    if [[ "${RUN_DOCKER}" == false ]]; then
        mark "Docker" "SKIP" "disabled by --noDocker"
        return
    fi
    if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
        if [[ "${RUN_DOCKER}" == true ]]; then
            mark "Docker" "FAIL" "docker requested but not available"
        else
            mark "Docker" "SKIP" "docker not available"
        fi
        return
    fi
    if [[ ! -f "${ROOT_DIR}/Dockerfile" ]]; then
        mark "Docker" "SKIP" "no Dockerfile yet"
        return
    fi
    log "Building the Docker image."
    if ! run_logged "${PIPELINE_LOG_DIR}/docker-build.log" docker build -t "${IMAGE_TAG}" "${ROOT_DIR}"; then
        mark "Docker" "FAIL" "image build failed"
        return
    fi
    log "Smoke testing the container (demo mode, persistent volume)."
    local port name volume
    port="$(free_port)"
    name="teampulse-pipeline-$$"
    volume="teampulse-pipeline-$$"
    docker run -d --rm --name "${name}" -p "127.0.0.1:${port}:8000" -v "${volume}:/data" \
        "${IMAGE_TAG}" demo --host 0.0.0.0 --port 8000 >/dev/null
    if wait_for_http "http://127.0.0.1:${port}/api/health" 90 \
        && curl -fsS "http://127.0.0.1:${port}/api/dashboard" | grep -q '"cards"' \
        && [[ "$(docker exec "${name}" id -u)" != "0" ]]; then
        local size
        size="$(docker image inspect "${IMAGE_TAG}" --format '{{.Size}}' | awk '{printf "%.0f MB", $1/1000000}')"
        mark "Docker" "PASS" "${IMAGE_TAG} (${size}) healthy, non-root"
    else
        mark "Docker" "FAIL" "container did not become healthy"
        docker logs "${name}" 2>&1 | tail -n 40 >&2
    fi
    docker rm -f "${name}" >/dev/null 2>&1
    docker volume rm "${volume}" >/dev/null 2>&1
}

open_report() {
    local report="${ROOT_DIR}/htmlcov/index.html"
    if [[ "${OPEN_REPORTS}" == false ]]; then
        mark "Open coverage" "SKIP" "disabled"
        return
    fi
    if [[ ! -f "${report}" ]]; then
        mark "Open coverage" "SKIP" "no report generated"
        return
    fi
    local opener
    for opener in "${TEAMPULSE_REPORT_BROWSER:-}" firefox xdg-open open; do
        if [[ -n "${opener}" ]] && command -v "${opener}" >/dev/null 2>&1; then
            "${opener}" "${report}" >/dev/null 2>&1 &
            disown || true
            mark "Open coverage" "PASS" "htmlcov/index.html handed to ${opener}"
            return
        fi
    done
    mark "Open coverage" "WARN" "no opener found; open ${report} manually"
}

launch_app() {
    if [[ "${RUN_APP}" == false ]]; then
        mark "Launch demo" "SKIP" "suppressed by --noRun"
        return
    fi
    if [[ "${FAILED}" -ne 0 ]]; then
        mark "Launch demo" "SKIP" "pipeline failed"
        return
    fi
    log "Starting the demo dashboard at http://127.0.0.1:8000/ (Ctrl+C to stop)."
    mark "Launch demo" "PASS" "uv run gitlab-team-pulse demo"
    print_summary
    exec uv run gitlab-team-pulse demo
}

print_summary() {
    {
        printf '\n========== Local Pipeline Summary ==========\n'
        printf 'version          : %s\n' "$(sed -nE 's/^__version__ = "(.*)"/\1/p' src/gitlab_team_pulse/__init__.py)"
        local line
        for line in "${SUMMARY_LINES[@]}"; do printf '%s\n' "${line}"; done
        printf '============================================\n'
        if [[ "${FAILED}" -eq 0 ]]; then printf 'RESULT: PASS\n'; else printf 'RESULT: FAIL\n'; fi
    } | tee "${PIPELINE_LOG_DIR}/summary.txt"
}

main() {
    parse_arguments "$@"
    prepare_report_directory
    if stage_sync; then
        stage_ruff
        stage_mypy
        stage_migrations
        stage_tests
        stage_e2e
        if stage_build; then stage_wheel_smoke; fi
        stage_docker
        open_report
    fi
    launch_app
    print_summary
    exit "${FAILED}"
}

main "$@"
