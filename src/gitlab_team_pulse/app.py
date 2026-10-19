"""FastAPI application factory: lifespan wiring, security headers, static UI and JSON API."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from gitlab_team_pulse import __version__, about, dashboard, db, meeting, store, teams
from gitlab_team_pulse.config import Settings
from gitlab_team_pulse.logging_setup import configure_logging, get_logger
from gitlab_team_pulse.models import Project, Team, User
from gitlab_team_pulse.scheduler import Scheduler
from gitlab_team_pulse.sync import ClientFactory, SyncService, default_client_factory

log = get_logger("web")

STATIC_DIR = Path(__file__).resolve().parent / "static"
CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "img-src 'self' data: https: http:",
        "style-src 'self'",
        "script-src 'self'",
        "connect-src 'self'",
        "font-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)


@dataclass
class AppContext:
    settings: Settings
    engine: Engine
    session_factory: sessionmaker[Session]
    service: SyncService
    scheduler: Scheduler


class SelectionUpdate(BaseModel):
    selected: bool


class TeamInput(BaseModel):
    name: str = Field(max_length=200)
    description: str = Field(default="", max_length=2000)
    member_ids: list[int] = Field(default_factory=list, max_length=500)
    scopes: list[str] = Field(default_factory=list, max_length=100)


def ctx(request: Request) -> AppContext:
    context: AppContext = request.app.state.ctx
    return context


def create_app(
    settings: Settings | None = None,
    *,
    client_factory: ClientFactory = default_client_factory,
) -> FastAPI:
    """Build the app. Startup migrates SQLite, serves the cache and starts the scheduler."""
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        token = settings.resolve_token()
        configure_logging(settings.log_level, secrets=[token.get_secret_value()] if token else [])
        log.info("starting gitlab-team-pulse %s, database %s", __version__, settings.database_path)
        engine = db.make_engine(settings.database_url)
        db.migrate(engine)
        session_factory = db.make_session_factory(engine)
        service = SyncService(settings, session_factory, client_factory=client_factory)
        scheduler = Scheduler(service)
        app.state.ctx = AppContext(settings, engine, session_factory, service, scheduler)
        if not settings.gitlab_configured:
            log.warning("GitLab is not configured; serving cached data only")
        if settings.scheduler_enabled:
            scheduler.start()
        try:
            yield
        finally:
            await scheduler.stop()
            await service.aclose()
            engine.dispose()
            log.info("stopped")

    app = FastAPI(
        title="GitLab Team Pulse",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        if not request.url.path.startswith("/api/docs"):
            response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    # The dashboard payload is large and repetitive JSON (about 10x smaller compressed); it is
    # refetched on every data change, so compression matters for browsers on a VPN.
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)
    app.include_router(api_router())
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    return app


def api_router() -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/health")
    def health(request: Request) -> JSONResponse:
        """Process health: the web app and SQLite. A GitLab outage is not unhealthy."""
        context = ctx(request)
        database = db.ping(context.engine)
        migrated = database and db.is_migrated(context.engine)
        body = {
            "status": "ok" if migrated else "unhealthy",
            "version": __version__,
            "database": database,
            "migrated": migrated,
            "gitlab_configured": context.settings.gitlab_configured,
        }
        return JSONResponse(body, status_code=200 if migrated else 503)

    @router.get("/about")
    def about_info() -> dict[str, Any]:
        """Version, author, license and runtime dependencies for the About dialog."""
        return about.about()

    @router.get("/status")
    def status(request: Request) -> dict[str, Any]:
        context = ctx(request)
        with context.session_factory() as session:
            return dashboard.build_status(
                session,
                context.settings,
                context.service.runtime,
                context.service.clock(),
                refresh_pending=context.scheduler.refresh_pending,
                next_selected_due=context.scheduler.next_selected_due,
            )

    @router.get("/users")
    def users(request: Request) -> dict[str, Any]:
        context = ctx(request)
        now = context.service.clock()
        with context.session_factory() as session:
            directory, _ = dashboard.global_freshness(
                session, context.service.runtime, context.settings, now
            )
            return {
                "data_version": store.data_version(session),
                "users_version": store.users_version(session),
                "directory": directory.as_dict(),
                "users": [
                    dashboard.user_payload(u, context.settings) for u in store.list_users(session)
                ],
            }

    @router.patch("/users/{user_id}/selection")
    async def update_selection(
        request: Request, user_id: int, update: SelectionUpdate
    ) -> dict[str, Any]:
        """Change the global selection; newly selected users sync right away."""
        context = ctx(request)
        with context.session_factory() as session:
            user = store.set_selected(session, user_id, update.selected, context.service.clock())
            if user is None:
                raise HTTPException(status_code=404, detail="unknown user")
            store.bump_data_version(session)
            store.bump_users_version(session)
            session.commit()
            payload = dashboard.user_payload(user, context.settings)
        if update.selected:
            context.scheduler.request_user_sync(user_id)
        log.info("user=%d selected=%s", user_id, update.selected)
        return payload

    @router.get("/dashboard")
    def full_dashboard(request: Request) -> dict[str, Any]:
        context = ctx(request)
        now = context.service.clock()
        with context.session_factory() as session:
            reader = dashboard.DashboardReader(
                session, context.settings, context.service.runtime, now
            )
            status_payload = dashboard.build_status(
                session,
                context.settings,
                context.service.runtime,
                now,
                refresh_pending=context.scheduler.refresh_pending,
                next_selected_due=context.scheduler.next_selected_due,
            )
            return {
                "generated_at": now,
                "data_version": status_payload["data_version"],
                "timezone": context.settings.timezone,
                "activity_days": context.settings.activity_days,
                "work_window_days": context.settings.work_window_days,
                "status": status_payload,
                **reader.overview(store.selected_users(session)),
            }

    def one_card(request: Request, user_id: int) -> dict[str, Any]:
        context = ctx(request)
        with context.session_factory() as session:
            user = session.get(User, user_id)
            if user is None:
                raise HTTPException(status_code=404, detail="unknown user")
            reader = dashboard.DashboardReader(
                session, context.settings, context.service.runtime, context.service.clock()
            )
            return reader.cards([user])[0]

    @router.get("/users/{user_id}/work")
    def user_work(request: Request, user_id: int) -> dict[str, Any]:
        card = one_card(request, user_id)
        return {"user": card["user"], "freshness": card["freshness"], "work": card["work"]}

    @router.get("/users/{user_id}/activity")
    def user_activity(request: Request, user_id: int) -> dict[str, Any]:
        card = one_card(request, user_id)
        return {
            "user": card["user"],
            "freshness": card["freshness"],
            "activity": card["activity"],
            "days": card["activity_days"],
        }

    @router.get("/users/{user_id}/time-summary")
    def user_time(request: Request, user_id: int) -> dict[str, Any]:
        card = one_card(request, user_id)
        return {"user": card["user"], "freshness": card["freshness"], **card["time"]}

    @router.get("/meeting-summary")
    def meeting_summary(
        request: Request,
        user_ids: str | None = Query(
            default=None, max_length=1000, description="Comma-separated user IDs"
        ),
        days: int = Query(default=7, ge=1, le=meeting.MAX_DAYS),
    ) -> dict[str, Any]:
        """The real outcome per person over the last ``days`` local days, built on demand from
        the cache (default people: the dashboard selection, in its order)."""
        context = ctx(request)
        try:
            wanted = [int(part) for part in (user_ids or "").split(",") if part.strip()]
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="user_ids must be numbers") from exc
        if len(wanted) > meeting.MAX_PEOPLE:
            raise HTTPException(
                status_code=422, detail=f"At most {meeting.MAX_PEOPLE} people per summary"
            )
        with context.session_factory() as session:
            if user_ids is None:
                people = store.selected_users(session)
            else:
                found = {u.id: u for u in session.scalars(select(User).where(User.id.in_(wanted)))}
                if missing := [uid for uid in wanted if uid not in found]:
                    raise HTTPException(
                        status_code=404, detail=f"Unknown users: {', '.join(map(str, missing))}"
                    )
                people = [found[uid] for uid in dict.fromkeys(wanted)]
            builder = meeting.MeetingSummary(
                session, context.settings, context.service.runtime, context.service.clock()
            )
            return builder.build(people, days)

    @router.get("/errors")
    def errors(
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
        include_resolved: bool = True,
    ) -> dict[str, Any]:
        context = ctx(request)
        with context.session_factory() as session:
            return {
                "unresolved": store.unresolved_error_count(session),
                "errors": dashboard.error_payloads(
                    session, limit=limit, include_resolved=include_resolved
                ),
            }

    def writable(context: AppContext) -> None:
        if context.settings.teams_readonly:
            raise HTTPException(
                status_code=403, detail="Teams are read-only here (TEAMPULSE_TEAMS_READONLY)"
            )

    def teams_body(context: AppContext, session: Session) -> dict[str, Any]:
        return {
            "teams_version": store.teams_version(session),
            "readonly": context.settings.teams_readonly,
            "teams": teams.team_payloads(session, context.settings),
        }

    def save(context: AppContext, team_id: int | None, body: TeamInput) -> dict[str, Any]:
        writable(context)
        now = context.service.clock()
        with context.session_factory() as session:
            team = None
            before: set[int] = set()
            if team_id is not None:
                team = session.get(Team, team_id)
                if team is None:
                    raise HTTPException(status_code=404, detail="unknown team")
                before = set(store.team_members(session, [team_id]).get(team_id, []))
            try:
                name, members, scopes = teams.validate_team(
                    session,
                    team_id=team_id,
                    name=body.name,
                    member_ids=body.member_ids,
                    scopes=body.scopes,
                )
            except teams.TeamError as exc:
                raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
            team = store.save_team(
                session,
                team,
                name=name,
                description=body.description.strip(),
                member_ids=members,
                scopes=scopes,
                now=now,
            )
            store.bump_teams_version(session)
            store.bump_data_version(session)
            session.commit()
            saved_id = team.id
            payload: dict[str, Any] = next(
                t for t in teams.team_payloads(session, context.settings) if t["id"] == saved_id
            )
        for user_id in sorted(set(members) - before):
            context.scheduler.request_user_sync(user_id)  # load the new member's history now
        log.info("team=%d saved with %d members", saved_id, len(members))
        return payload

    @router.get("/projects")
    def projects(request: Request) -> dict[str, Any]:
        """Known project paths and their namespaces: suggestions for a team's scope."""
        context = ctx(request)
        with context.session_factory() as session:
            paths = sorted(session.scalars(select(Project.path_with_namespace)))
        namespaces = sorted(
            {"/".join(p.split("/")[:i]) for p in paths for i in range(1, p.count("/") + 1)}
        )
        return {"projects": paths, "namespaces": namespaces}

    @router.get("/teams")
    def list_teams(request: Request) -> dict[str, Any]:
        context = ctx(request)
        with context.session_factory() as session:
            return teams_body(context, session)

    @router.post("/teams", status_code=201)
    def create_team(request: Request, body: TeamInput) -> dict[str, Any]:
        return save(ctx(request), None, body)

    @router.put("/teams/{team_id}")
    def update_team(request: Request, team_id: int, body: TeamInput) -> dict[str, Any]:
        return save(ctx(request), team_id, body)

    @router.delete("/teams/{team_id}", status_code=204)
    def delete_team(request: Request, team_id: int) -> Response:
        context = ctx(request)
        writable(context)
        with context.session_factory() as session:
            team = session.get(Team, team_id)
            if team is None:
                raise HTTPException(status_code=404, detail="unknown team")
            session.delete(team)
            store.bump_teams_version(session)
            store.bump_data_version(session)
            session.commit()
        log.info("team=%d deleted", team_id)
        return Response(status_code=204)

    @router.post("/teams/{team_id}/apply")
    def apply_team(request: Request, team_id: int) -> dict[str, Any]:
        """Make the team's members the shared dashboard selection."""
        context = ctx(request)
        with context.session_factory() as session:
            if session.get(Team, team_id) is None:
                raise HTTPException(status_code=404, detail="unknown team")
            members = set(store.team_members(session, [team_id]).get(team_id, []))
            added, removed = store.apply_selection(session, members, context.service.clock())
            store.bump_data_version(session)
            store.bump_users_version(session)
            session.commit()
        for user_id in sorted(added):
            context.scheduler.request_user_sync(user_id)
        log.info("team=%d applied: %d selected, %d deselected", team_id, len(added), removed)
        return {"selected": len(members), "added": len(added), "removed": removed}

    @router.get("/teams/{team_id}/report")
    def team_report(
        request: Request,
        team_id: int,
        period: teams.Period = "month",
        offset: int = Query(default=0, ge=0, le=60),
    ) -> dict[str, Any]:
        context = ctx(request)
        with context.session_factory() as session:
            reporter = teams.TeamReporter(session, context.settings, context.service.clock())
            report = reporter.report(team_id, period, offset)
        if report is None:
            raise HTTPException(status_code=404, detail="unknown team")
        return report

    @router.get("/teams/{team_id}/digest")
    def team_digest(request: Request, team_id: int) -> dict[str, Any]:
        context = ctx(request)
        with context.session_factory() as session:
            result = teams.digest(session, context.settings, context.service.clock(), team_id)
        if result is None:
            raise HTTPException(status_code=404, detail="unknown team")
        return result

    @router.post("/refresh", status_code=202)
    async def refresh(request: Request) -> JSONResponse:
        """Schedule a refresh and return immediately; progress is visible via /api/status."""
        result = ctx(request).scheduler.request_refresh()
        body = {"accepted": result.accepted, "status": result.status}
        if not result.accepted:
            body["retry_after_seconds"] = result.retry_after_seconds
            return JSONResponse(
                body,
                status_code=429,
                headers={"Retry-After": str(max(1, round(result.retry_after_seconds)))},
            )
        return JSONResponse(body, status_code=202)

    return router
