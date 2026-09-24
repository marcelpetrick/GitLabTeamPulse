"""Command line entry point: ``gitlab-team-pulse serve|migrate|refresh|doctor|demo|fake-gitlab``."""

from __future__ import annotations

import argparse
import asyncio
import sys
import threading
import time
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

import uvicorn
from pydantic import SecretStr, ValidationError

from gitlab_team_pulse import __version__, db, store
from gitlab_team_pulse.app import create_app
from gitlab_team_pulse.config import Settings, default_database_path
from gitlab_team_pulse.fake_gitlab import FAKE_TOKEN, create_fake_gitlab
from gitlab_team_pulse.gitlab.errors import GitLabError
from gitlab_team_pulse.logging_setup import configure_logging
from gitlab_team_pulse.sync import ClientFactory, SyncService, default_client_factory, utcnow

OK, WARN, FAIL = "ok", "warn", "fail"


def _load_settings(**overrides: object) -> Settings:
    settings = Settings()
    return settings.model_copy(update={k: v for k, v in overrides.items() if v is not None})


def cmd_serve(args: argparse.Namespace) -> int:
    settings = _load_settings(host=args.host, port=args.port)
    configure_logging(settings.log_level)
    # One worker only: the in-process scheduler must have exactly one owner.
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        workers=1,
        log_config=None,
        proxy_headers=True,
    )
    return 0


def cmd_migrate(_args: argparse.Namespace) -> int:
    settings = _load_settings()
    configure_logging(settings.log_level)
    engine = db.make_engine(settings.database_url)
    db.migrate(engine)
    print(f"database {settings.database_path} is at revision {db.current_revision(engine)}")
    engine.dispose()
    return 0


async def _refresh(settings: Settings, client_factory: ClientFactory) -> tuple[str, str]:
    engine = db.make_engine(settings.database_url)
    db.migrate(engine)
    service = SyncService(settings, db.make_session_factory(engine), client_factory=client_factory)
    try:
        directory = await service.sync_directory("manual")
        selected = await service.sync_selected("manual")
    finally:
        await service.aclose()
        engine.dispose()
    return directory, selected


def cmd_refresh(_args: argparse.Namespace) -> int:
    settings = _load_settings()
    configure_logging(settings.log_level)
    directory, selected = asyncio.run(_refresh(settings, default_client_factory))
    print(f"directory: {directory}\nselected users: {selected}")
    return 0 if directory == "ok" and selected in {"ok", "partial"} else 1


async def _doctor_gitlab(
    settings: Settings, client_factory: ClientFactory
) -> list[tuple[str, str, str]]:
    checks: list[tuple[str, str, str]] = []
    try:
        client = client_factory(settings)
    except GitLabError as exc:
        return [(FAIL, "gitlab", str(exc))]
    try:
        version = await client.version()
        checks.append((OK, "gitlab", f"reachable, version {version}"))
        me = await client.current_user()
        if me.is_admin:
            checks.append((OK, "token", f"authenticated as {me.username} (administrator)"))
        else:
            checks.append(
                (
                    WARN,
                    "token",
                    f"authenticated as {me.username}; not an admin, directory may be incomplete",
                )
            )
        now = utcnow()
        try:
            await client.get_user_timelogs(me.username, now - timedelta(days=1), now)
            checks.append((OK, "timelogs", "GraphQL timelogs query works"))
        except GitLabError as exc:
            checks.append((WARN, "timelogs", f"unavailable: {exc}"))
    except GitLabError as exc:
        checks.append((FAIL, "gitlab", str(exc)))
    finally:
        await client.aclose()
    return checks


def run_doctor(settings: Settings, client_factory: ClientFactory) -> list[tuple[str, str, str]]:
    checks: list[tuple[str, str, str]] = []
    checks.append(
        (
            OK if settings.gitlab_url else FAIL,
            "config",
            f"GitLab URL: {settings.gitlab_url or 'not set'}",
        )
    )
    token = settings.resolve_token()
    checks.append((OK if token else FAIL, "config", "token present" if token else "token missing"))
    engine = db.make_engine(settings.database_url)
    try:
        if not db.ping(engine):
            checks.append((FAIL, "database", f"cannot open {settings.database_path}"))
        elif db.is_migrated(engine):
            checks.append(
                (OK, "database", f"{settings.database_path} at {db.current_revision(engine)}")
            )
        else:
            checks.append(
                (
                    WARN,
                    "database",
                    f"{settings.database_path} needs migration (run `gitlab-team-pulse migrate`)",
                )
            )
    finally:
        engine.dispose()
    if settings.gitlab_url and token:
        checks.extend(asyncio.run(_doctor_gitlab(settings, client_factory)))
    return checks


def cmd_doctor(_args: argparse.Namespace) -> int:
    settings = _load_settings()
    checks = run_doctor(settings, default_client_factory)
    for level, area, message in checks:
        print(f"[{level.upper():4}] {area:9} {message}")
    return 1 if any(level == FAIL for level, _, _ in checks) else 0


class _ThreadedServer(uvicorn.Server):
    def install_signal_handlers(self) -> None:  # pragma: no cover - thread has no signals
        pass


def start_fake_gitlab(host: str, port: int) -> uvicorn.Server:
    """Run the fake GitLab in a daemon thread and wait until it accepts requests."""
    config = uvicorn.Config(create_fake_gitlab(), host=host, port=port, log_config=None)
    server = _ThreadedServer(config)
    threading.Thread(target=server.run, daemon=True, name="fake-gitlab").start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    return server


async def _seed_demo(settings: Settings, select: int) -> None:
    engine = db.make_engine(settings.database_url)
    db.migrate(engine)
    session_factory = db.make_session_factory(engine)
    service = SyncService(settings, session_factory)
    try:
        await service.sync_directory("startup")
        with session_factory() as session:
            if store.user_counts(session)[1] == 0:
                humans = [
                    u
                    for u in store.list_users(session)
                    if u.account_type == "human" and u.state == "active"
                ]
                for user in sorted(humans, key=lambda u: u.id)[1 : select + 1]:
                    store.set_selected(session, user.id, True, service.clock())
                store.bump_data_version(session)
                store.bump_users_version(session)
                session.commit()
    finally:
        await service.aclose()
        engine.dispose()


def demo_settings(args: argparse.Namespace) -> Settings:
    database = (
        Path(args.database) if args.database else default_database_path().with_name("demo.db")
    )
    return Settings(
        _env_file=None,
        gitlab_url=f"http://{args.gitlab_host}:{args.gitlab_port}",
        gitlab_token=SecretStr(FAKE_TOKEN),
        gitlab_token_file=Path("/nonexistent/teampulse-demo-token"),
        database_path=database,
        host=args.host,
        port=args.port,
        selected_refresh_interval_seconds=args.refresh_seconds,
        manual_refresh_min_interval_seconds=3,
    )


def cmd_demo(args: argparse.Namespace) -> int:
    settings = demo_settings(args)
    configure_logging(settings.log_level)
    start_fake_gitlab(args.gitlab_host, args.gitlab_port)
    asyncio.run(_seed_demo(settings, args.select))
    print(
        f"demo dashboard: http://{settings.host}:{settings.port}/ "
        f"(fake GitLab on {settings.gitlab_url})"
    )
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_config=None)
    return 0


def cmd_fake_gitlab(args: argparse.Namespace) -> int:
    configure_logging("INFO")
    print(f"fake GitLab on http://{args.host}:{args.port} (token: {FAKE_TOKEN})")
    uvicorn.run(create_fake_gitlab(), host=args.host, port=args.port, log_config=None)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gitlab-team-pulse", description="People-centric GitLab activity dashboard."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="run the web dashboard (single worker)")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.set_defaults(func=cmd_serve)

    commands.add_parser("migrate", help="create or upgrade the SQLite schema").set_defaults(
        func=cmd_migrate
    )
    commands.add_parser("refresh", help="run one directory and selected-user sync").set_defaults(
        func=cmd_refresh
    )
    commands.add_parser("doctor", help="check configuration, database and GitLab").set_defaults(
        func=cmd_doctor
    )

    demo = commands.add_parser("demo", help="run the dashboard against a built-in fake GitLab")
    demo.add_argument("--host", default="127.0.0.1")
    demo.add_argument("--port", type=int, default=8000)
    demo.add_argument("--gitlab-host", default="127.0.0.1")
    demo.add_argument("--gitlab-port", type=int, default=8081)
    demo.add_argument("--database", help="SQLite path (default: demo.db next to the normal one)")
    demo.add_argument("--select", type=int, default=4, help="users to pre-select on first run")
    demo.add_argument("--refresh-seconds", type=int, default=600)
    demo.set_defaults(func=cmd_demo)

    fake = commands.add_parser("fake-gitlab", help="run only the fake GitLab API")
    fake.add_argument("--host", default="127.0.0.1")
    fake.add_argument("--port", type=int, default=8081)
    fake.set_defaults(func=cmd_fake_gitlab)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ValidationError as exc:
        print(f"invalid configuration:\n{exc}", file=sys.stderr)
        return 2
