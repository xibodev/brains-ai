from contextlib import asynccontextmanager

from fastapi import FastAPI

from brains.admin import router as admin_router
from brains.api.admin_key import ensure_admin_key
from brains.api.anthropic import router as a
from brains.api.coordination import router as coordination_router
from brains.api.errors import register_exception_handlers
from brains.api.health import router as h
from brains.api.issues import router as issues_router
from brains.api.models import router as m
from brains.api.onboarding import router as onboarding_router
from brains.api.openai import router as o
from brains.api.orgs import router as orgs_router
from brains.api.personas import router as personas_router
from brains.api.projects import router as projects_router
from brains.api.runtimes import enrol_public as rt_enrol_public
from brains.api.runtimes import router as rt
from brains.api.webhooks import router as wh
from brains.api.ws import router as ws_router
from brains.authz.deps import install_principal_context
from brains.config import settings
from brains.observability import configure_otel
from brains.observability.dump import install as install_dump
from brains.storage.migrations import init_db
from brains.web import mount_static
from brains.web.spa import mount_favicon, mount_spa


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    ensure_admin_key(print_banner=True)
    # Auto-provision / sync the ``admin`` operator so every session
    # started against this gateway can stamp a valid foreign key. See the
    # identity and trust boundaries in ``docs/ARCHITECTURE.md``.
    from brains.control.operators import ensure_admin_operator

    ensure_admin_operator()
    yield


app = FastAPI(lifespan=lifespan)
configure_otel(app, settings)
register_exception_handlers(app)
# BL-P0-01 — reset the request-scoped principal ContextVar when each request
# finishes, so an authenticated actor never leaks into the next request served
# by the same worker.
install_principal_context(app)
mount_static(app)
# Native-battalion operator SPA (Vite/React build, served by FastAPI; auth-gated
# index + history fallback). Graceful no-op if the bundle isn't built.
mount_spa(app)
mount_favicon(app)
app.include_router(h)
app.include_router(m)
app.include_router(o)
app.include_router(wh)
app.include_router(rt)
# Unauthenticated enrolment redeem (the token is the credential, F1.2). Carries
# ONLY POST /v1/runtimes/enrol/redeem — no other routes are exposed unauthed.
app.include_router(rt_enrol_public)
app.include_router(a)
app.include_router(admin_router)
# Native-battalion WS3 — REST CRUD + action + realtime surface. Every route
# resolves one principal and applies an explicit Org/Workspace capability check
# (``brains.authz``); the WS/SSE handlers reuse the same resolver.
app.include_router(orgs_router)
app.include_router(personas_router)
app.include_router(projects_router)
app.include_router(issues_router)
app.include_router(onboarding_router)
app.include_router(coordination_router)
app.include_router(ws_router)
# Optional LLM I/O dump for investigation. No-op unless ``BRAINS_DUMP_DIR``
# env var is set. See ``brains.observability.dump``.
install_dump(app)


# Copilot CLI compatibility — the GitHub Copilot CLI appends bare paths
# (``/chat/completions``, ``/responses``, ``/models``) to its
# ``COPILOT_API_URL`` base rather than ``/v1/...``. Rewrite them so the
# same handlers serve both shapes. Added LAST so this middleware runs
# BEFORE the dump middleware, meaning records are tagged with the
# canonical ``/v1/*`` path.
_COPILOT_PATH_ALIASES = {
    "/chat/completions": "/v1/chat/completions",
    "/responses": "/v1/responses",
    "/completions": "/v1/chat/completions",
    "/models": "/v1/models",
}


@app.middleware("http")
async def _copilot_path_alias(request, call_next):
    target = _COPILOT_PATH_ALIASES.get(request.url.path)
    if target is not None:
        request.scope["path"] = target
        request.scope["raw_path"] = target.encode("ascii")
    return await call_next(request)
