from contextlib import asynccontextmanager

from fastapi import FastAPI

from brains.admin import router as admin_router
from brains.api.admin_key import ensure_admin_key
from brains.api.coordination import router as coordination_router
from brains.api.errors import register_exception_handlers
from brains.api.health import router as h
from brains.api.operator import router as operator_router
from brains.api.orgs import router as orgs_router
from brains.api.ws import router as ws_router
from brains.authz.deps import install_principal_context
from brains.capabilities import withdrawn_http_path
from brains.storage.migrations import init_db
from brains.web.spa import mount_favicon, mount_spa


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    ensure_admin_key(print_banner=True)
    # Auto-provision / sync the ``admin`` operator so every session
    # started against this gateway can stamp a valid foreign key. See the
    # identity and trust boundaries in ``docs/ARCHITECTURE.md``.
    from brains.control.durable_mailbox import ensure_operator_mailboxes
    from brains.control.operators import ensure_admin_operator

    ensure_admin_operator()
    ensure_operator_mailboxes()
    yield


app = FastAPI(lifespan=lifespan)
register_exception_handlers(app)
# BL-P0-01 — reset the request-scoped principal ContextVar when each request
# finishes, so an authenticated actor never leaks into the next request served
# by the same worker.
install_principal_context(app)
# Native-battalion operator SPA (Vite/React build, served by FastAPI; auth-gated
# index + history fallback). Graceful no-op if the bundle isn't built.
mount_spa(app)
mount_favicon(app)
app.include_router(h)

# Remove interleaved frozen routes before inclusion so both dispatch and the
# generated OpenAPI document share the same boundary.
for core_router in (admin_router, orgs_router, coordination_router, operator_router):
    core_router.routes[:] = [
        route
        for route in core_router.routes
        if not withdrawn_http_path(getattr(route, "path", ""))
    ]

app.include_router(admin_router)
# Native-battalion WS3 — REST CRUD + action + realtime surface. Every route
# resolves one principal and applies an explicit Org/Workspace capability check
# (``brains.authz``); the WS/SSE handlers reuse the same resolver.
app.include_router(orgs_router)
app.include_router(coordination_router)
app.include_router(operator_router)
app.include_router(ws_router)
