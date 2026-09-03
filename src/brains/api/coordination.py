"""Coordination REST router (WS3 §1.6 + §2).

The ask_human + gate surface and the session control surface, as a thin HTTP
shell over the existing control functions — it does **not** reimplement the gate
or the ask_human ticket loop:

* ``/v1/asks/*``      → list open asks; answer a ticket → ``resolve_decision``.
* ``/v1/approvals/*`` → list pending gated requests; approve/deny → ``resolve_decision``.
* ``/v1/sessions``    → operator/board reads over ``agent_sessions``.
* ``/v1/sessions/spawn`` → enqueue a spawn order via ``control.assignments`` (queued +
  daemon-pull; the gate stays intact inside the spawned session).
* ``/v1/sessions/{id}/message`` + ``/stop`` → record a durable Session command
  via ``control.session_commands`` (BL-P0-05): persisted before it is
  announced, idempotent per operation key, claimed by exactly one consumer,
  and settled with the outcome that consumer actually observed.

Two verb paths (``/asks`` + ``/approvals``) over one ``ApprovalRequest`` store
(DESIGN-SYNTHESIS fork WS3-3).

Authorization is explicit per route. Reads are scoped to the Workspaces the
principal can see; resolving an approval additionally requires ``member`` in
the approval's Org **and** passes the separation-of-duty check in
:func:`brains.control.decisions.assert_resolver_allowed`, so an agent cannot
approve its own ASK. Install-wide surfaces (gateway usage totals, provider
configuration) are restricted to the bootstrap admin, because they are not
Org-attributed and returning them to an Org member would leak another Org's
activity.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from brains.api.pagination import paginate
from brains.api.realtime_publish import publish_inbox, publish_session
from brains.authz import policy
from brains.authz.deps import require_console_principal, require_operator_principal
from brains.authz.principal import CAP_ORG_READ, CAP_ORG_WRITE, Principal
from brains.control import assignments as assignments_ctl
from brains.control import decisions as decisions_ctl
from brains.control import session_commands as commands_ctl
from brains.control import sessions as sessions_ctl

router = APIRouter(prefix="/v1")


class SpawnBody(BaseModel):
    issue_id: int | None = None
    persona_id: int | None = None
    runtime_id: str | int | None = None
    prompt: str | None = None


class ResolveBody(BaseModel):
    decision: str | None = None
    chosen: str | None = None
    reasoning: str | None = None
    status: str | None = None
    session_id: str | None = None


class ApprovalRouteBody(BaseModel):
    assigned_operator: str | None = None
    clear_assignment: bool = False
    priority: str | None = None
    due_at: datetime | None = None
    clear_due: bool = False
    escalation_level: int | None = Field(default=None, ge=0)
    escalation_reason: str = ""


class ApprovalEscalateBody(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)
    assigned_operator: str | None = None
    due_at: datetime | None = None


class AnswerBody(BaseModel):
    answer: str
    reasoning: str | None = None
    session_id: str | None = None


class SecureSettingBody(BaseModel):
    value: str = Field(min_length=1, max_length=4096)


class MailTestBody(BaseModel):
    to: str = Field(min_length=3, max_length=320)


class GeneralConfigurationBody(BaseModel):
    updates: dict[str, Any]


def _bad_request(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _resolve_to_status(body: ResolveBody) -> tuple[str, str, str]:
    """Return ``(chosen, reasoning, status)`` from an approve/deny or explicit body."""
    decision = (body.decision or "").strip().lower()
    if body.status is not None:
        status = body.status
    elif decision in {"deny", "reject", "rejected", "no"}:
        status = "rejected"
    elif decision in {"defer", "deferred"}:
        status = "deferred"
    else:
        status = "resolved"
    chosen = body.chosen if body.chosen is not None else (body.decision or "approve")
    return chosen, body.reasoning or "", status


def _authorized_approval(principal: Principal, code: str, capability: str) -> None:
    """Authorize the principal against the Org *and* Workspace of an approval.

    Workspace visibility is part of the check, not only Org membership: a
    ``private`` Workspace is filtered out of ``/v1/approvals``, so answering
    for one of its approvals here would make the detail surface disclose what
    the listing hides.
    """
    workspace_id = policy.approval_workspace_id(code)
    if workspace_id is None:
        raise policy.not_found("approval", code)
    policy.require_workspace_capability(
        principal, capability, workspace_id, entity="approval", ref=code
    )


def _resolve_approval(
    principal: Principal,
    code: str,
    chosen: str,
    reasoning: str,
    status: str,
    resolving_session_id: str | None,
) -> dict:
    _authorized_approval(principal, code, CAP_ORG_WRITE)
    try:
        return decisions_ctl.resolve_decision(
            code,
            chosen,
            reasoning=reasoning,
            status=status,
            principal=principal,
            resolving_session_id=resolving_session_id,
        )
    except decisions_ctl.ApprovalAuthorizationError as exc:
        raise policy.forbidden(str(exc)) from exc
    except ValueError as exc:
        msg = str(exc)
        if "unknown decision" in msg:
            raise policy.not_found("approval", code) from exc
        if "not open" in msg:
            raise HTTPException(status_code=409, detail=msg) from exc
        raise _bad_request(exc) from exc


def _authorized_session(principal: Principal, session_id: str, capability: str) -> dict:
    """Resolve a Session and authorize its Org *and* Workspace visibility.

    ``/v1/sessions`` filters by :func:`policy.visible_workspace_ids`, which
    honours ``private`` Workspaces; the per-ID surfaces must agree, or a
    Session absent from the listing would still be readable by id.
    """
    row = sessions_ctl.get_agent_session(session_id)
    if row is None:
        raise policy.not_found("session", session_id)
    policy.require_workspace_capability(
        principal,
        capability,
        row.get("workspace_id"),
        entity="session",
        ref=session_id,
    )
    return row


def _scope_sessions(principal: Principal, rows: list[dict]) -> list[dict]:
    return policy.scope_sessions(principal, rows)


# --------------------------------------------------------------------------- #
# Asks (ask_human tickets)
# --------------------------------------------------------------------------- #


@router.get("/asks")
def list_asks(
    limit: int | None = None,
    cursor: str | None = None,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    rows = decisions_ctl.list_open_decisions()
    return paginate(rows, limit=limit, cursor=cursor)


@router.post("/asks/{code}/answer")
def answer_ask(
    code: str,
    body: AnswerBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    result = _resolve_approval(
        principal, code, body.answer, body.reasoning or "", "resolved", body.session_id
    )
    publish_inbox(None, "ask_human.answered", result)
    return result


# --------------------------------------------------------------------------- #
# Handoffs (read; surfaced in the inbox "Handoffs" tab)
# --------------------------------------------------------------------------- #


@router.get("/handoffs")
def list_handoffs(
    status: str | None = "active",
    limit: int | None = None,
    cursor: str | None = None,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    """Active session handoffs awaiting a human picker (WS4 §3.1).

    Handoffs are workspace-scoped infra surfaced in the inbox; this read honours
    the Org/Workspace visibility filter inside the control layer. ``status``
    defaults to ``active``; pass any other value to include the full history.
    """
    from brains.control import handoffs as handoffs_ctl

    active_only = (status or "").lower() in {"", "active"}
    rows = handoffs_ctl.list_handoffs(active_only=active_only)
    shaped = [
        {
            "id": r.get("handoff_id"),
            "title": r.get("title"),
            "body": r.get("body"),
            "status": r.get("status"),
            "workspace": r.get("workspace"),
            "created_at": r.get("set_at"),
        }
        for r in rows
    ]
    if status and status.lower() not in {"", "active"}:
        shaped = [r for r in shaped if r["status"] == status]
    return paginate(shaped, limit=limit, cursor=cursor)


# --------------------------------------------------------------------------- #
# Approvals (gated decision queue)
# --------------------------------------------------------------------------- #


@router.get("/approvals")
def list_approvals(
    kind: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    rows = decisions_ctl.list_open_decisions()
    return paginate(rows, limit=limit, cursor=cursor)


@router.get("/usage")
def usage_summary(
    days: int = 30,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    """Usage dashboard data (F9): token + cost totals and the top routed models
    over the window, from the gateway usage ledger.

    The ledger records gateway calls install-wide and carries no Org
    attribution, so it is returned only to the bootstrap admin. Handing
    install-wide totals to one Org's member would disclose another Org's
    activity; per-Org attribution is tracked as an open gap in
    ``docs/product/FEATURE_CONTRACT.md`` (AC-F9-04).
    """
    if not principal.is_bootstrap_admin:
        raise policy.forbidden(
            "gateway usage totals are install-wide and not Org-attributed; "
            "they are available to the bootstrap admin only"
        )
    from brains.router import savings

    try:
        totals = savings.totals(days=days)
    except Exception:
        totals = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
    try:
        top_models = savings.top_routed_models(days=days)
    except Exception:
        top_models = []
    return {"days": days, "scope": "gateway", "totals": totals, "top_models": top_models}


@router.get("/config/summary")
def config_summary(principal: Principal = Depends(require_operator_principal)) -> dict:
    """Real, redacted console config (F7) — de-stubs the Config tabs. Lists
    configured providers (real vs stub), gateway/router posture, and counts. No
    secrets are returned; provider keys stay in the secure env/admin surface.

    Install-level configuration, so it is admin-only: which providers an
    install has configured is not an Org-scoped fact."""
    if not principal.is_bootstrap_admin:
        raise policy.forbidden("gateway configuration is available to the bootstrap admin only")
    from brains.admin.service import provider_status_view
    from brains.config import settings
    from brains.providers.registry import is_stub_provider

    providers = []
    for provider in provider_status_view():
        stub = bool(provider["is_stub"])
        configured = bool(provider["configured"]) and not stub
        providers.append(
            {
                "name": provider["name"],
                "configured": configured,
                "stub": stub,
                "status": (
                    "simulated" if stub else ("configured" if configured else "unconfigured")
                ),
                "reason": provider["reason"],
            }
        )
    models = [
        {
            "tier": tier,
            "provider": route.provider,
            "model": route.model,
            "simulated": is_stub_provider(route.provider),
        }
        for tier, route in settings.models.items()
    ]
    gateway = {}
    try:
        from brains.config import settings

        gateway = {
            "router_enabled": bool(getattr(getattr(settings, "router", None), "enabled", False)),
            "base_url": "/v1",
        }
    except Exception:
        gateway = {"base_url": "/v1"}
    github_binding_count = sum(
        1
        for entry in settings.github_repository_org_bindings
        if "=" in entry and all(part.strip() for part in entry.split("=", 1))
    )
    integrations: dict[str, object] = {
        "github": {
            "configured": bool(settings.github_webhook_secret and github_binding_count),
            "allowed_repository_count": github_binding_count,
        },
        "bridges": [],
    }
    bridge_rows = []
    for bridge_name in ("whatsapp", "whatsapp_web", "telegram", "slack"):
        module = __import__(
            f"brains.bridges.{bridge_name}",
            fromlist=["status"],
        )
        configured = bool(module.status(settings).configured)
        bridge_rows.append(
            {
                "name": bridge_name,
                "configured": configured,
                "status": "configured" if configured else "unconfigured",
            }
        )
    from brains.storage.db import SessionLocal
    from brains.storage.models import IntegrationDelivery

    with SessionLocal() as session:
        delivery_states: dict[str, str] = {}
        rows = (
            session.query(IntegrationDelivery.channel, IntegrationDelivery.status)
            .filter(
                IntegrationDelivery.direction == "outbound",
            )
            .order_by(IntegrationDelivery.updated_at.desc())
            .all()
        )
        for channel, status in rows:
            delivery_states.setdefault(channel, status)
    for bridge in bridge_rows:
        bridge_name = str(bridge["name"])
        if bridge["configured"] and delivery_states.get(bridge_name) == "failed":
            bridge["status"] = "degraded"
    integrations["bridges"] = bridge_rows
    return {
        "providers": providers,
        "gateway": gateway,
        "models": models,
        "routes": dict(settings.routes),
        "integrations": integrations,
        "write_contract": {
            "mode": "bounded_writes",
            "detail": (
                "Provider/gateway configuration is read-only. The Email section performs "
                "bounded encrypted writes and never returns secret plaintext."
            ),
            "reload": (
                "Legacy overlay or environment writes reload only the process that handled "
                "the write; restart every Brains process before treating the change as active."
            ),
        },
        "models_endpoint": "/v1/models",
        "secrets_managed": (
            "Email secrets are encrypted in the Brains database and editable only through "
            "the protected Email section. Process environment values remain higher priority."
        ),
    }


@router.get("/admin/configuration/email")
def email_configuration(
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    if not principal.is_bootstrap_admin:
        raise policy.forbidden("email configuration is available to the bootstrap admin only")
    from brains.api.admin_key import ensure_admin_key
    from brains.control.mailer import mailer_status
    from brains.control.secure_settings import status

    key, _ = ensure_admin_key(print_banner=False)
    secure = status(key)
    import os

    for name, row in secure["settings"].items():
        if f"BRAINS_{name.upper()}" in os.environ:
            row["set"] = True
            row["source"] = "environment"
    return {"mailer": mailer_status(), "secure": secure}


@router.get("/admin/configuration/secrets")
def secret_configuration(
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    if not principal.is_bootstrap_admin:
        raise policy.forbidden("secret configuration is available to the bootstrap admin only")
    from brains.api.admin_key import ensure_admin_key
    from brains.control.secure_settings import SECRET_NAMES, source_for, status

    key, _ = ensure_admin_key(print_banner=False)
    secure = status(key)
    settings_rows = {name: row for name, row in secure["settings"].items() if name in SECRET_NAMES}
    encrypted_names = {name for name, row in settings_rows.items() if row["source"] == "encrypted"}
    for name, row in settings_rows.items():
        row["source"] = source_for(name, encrypted_names)
        row["set"] = row["source"] != "unset"
    return {"encrypted_store": secure["encrypted_store"], "settings": settings_rows}


@router.put("/admin/configuration/secrets/{name}")
def set_secret_configuration(
    name: str,
    body: SecureSettingBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    if not principal.is_bootstrap_admin:
        raise policy.forbidden("secret configuration is available to the bootstrap admin only")
    from brains.api.admin_key import ensure_admin_key
    from brains.audit import required_effect
    from brains.config import reload_settings
    from brains.control.secure_settings import SECRET_NAMES, set_value

    if name not in SECRET_NAMES:
        raise _bad_request(ValueError(f"unsupported encrypted secret setting: {name}"))
    key, _ = ensure_admin_key(print_banner=False)
    with required_effect(
        actor="admin", action="admin.secure_setting_write", payload={"name": name}
    ):
        result = set_value(name, body.value, admin_key=key)
    reload_settings()
    return result


@router.delete("/admin/configuration/secrets/{name}")
def clear_secret_configuration(
    name: str,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    if not principal.is_bootstrap_admin:
        raise policy.forbidden("secret configuration is available to the bootstrap admin only")
    from brains.api.admin_key import ensure_admin_key
    from brains.audit import required_effect
    from brains.config import reload_settings
    from brains.control.secure_settings import SECRET_NAMES, clear_value

    if name not in SECRET_NAMES:
        raise _bad_request(ValueError(f"unsupported encrypted secret setting: {name}"))
    ensure_admin_key(print_banner=False)
    with required_effect(
        actor="admin", action="admin.secure_setting_clear", payload={"name": name}
    ):
        result = clear_value(name)
    reload_settings()
    return result


@router.get("/admin/configuration/general")
def general_configuration(
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    if not principal.is_bootstrap_admin:
        raise policy.forbidden("configuration is available to the bootstrap admin only")
    from brains.admin.service import current_config_view, read_overlay
    from brains.config import settings

    overlay = dict(read_overlay())
    # A literal legacy API key must not be echoed into the new editor. The
    # encrypted Secrets section is the only supported browser write path.
    overlay.pop("openai_compatible_api_key", None)
    return {
        "live": current_config_view(),
        "overlay": overlay,
        "overlay_path": settings.runtime_overlay,
    }


@router.put("/admin/configuration/general")
def set_general_configuration(
    body: GeneralConfigurationBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    if not principal.is_bootstrap_admin:
        raise policy.forbidden("configuration is available to the bootstrap admin only")
    from brains.admin.service import (
        current_config_view,
        parse_control_payload,
        parse_models_payload,
        parse_router_payload,
        parse_routes_payload,
        parse_savings_payload,
        write_overlay,
    )
    from brains.audit import required_effect

    source = body.updates
    try:
        updates: dict[str, Any] = {}
        if "models" in source:
            updates["models"] = parse_models_payload(source["models"])
        known_tiers = set((updates.get("models") or current_config_view()["models"]).keys())
        if "routes" in source:
            updates["routes"] = parse_routes_payload(source["routes"], known_tiers)
        if "control" in source:
            updates["control"] = parse_control_payload(source["control"])
        if "router" in source:
            updates["router"] = parse_router_payload(source["router"])
        if "savings" in source:
            updates["savings"] = parse_savings_payload(source["savings"])
        for scalar in (
            "subsystems",
            "rate_limit_per_minute",
            "ollama_base_url",
            "ollama_timeout_seconds",
            "openai_compatible_base_url",
            "openai_compatible_timeout_seconds",
            "litellm_timeout_seconds",
            "source_allowlist",
            "context_compression_enabled",
            "savings_holdout_fraction",
            "trace_max_payload_bytes",
            "trace_retention_max_rows",
            "provider_policies",
            "github_copilot_use_gh_cli",
            "github_copilot_cache_dir",
            "github_copilot_timeout_seconds",
            "github_copilot_editor_version",
            "github_copilot_integration_id",
            "allow_copilot_proxy",
            "gateway_preamble",
            "embed_model",
        ):
            if scalar in source:
                updates[scalar] = source[scalar]
        with required_effect(
            actor="admin",
            action="admin.overlay_write",
            payload={"keys": sorted(updates)},
        ):
            overlay = write_overlay(updates)
        return {"overlay": overlay, "live": current_config_view()}
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.put("/admin/configuration/email/{name}")
def set_email_configuration(
    name: str,
    body: SecureSettingBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    if not principal.is_bootstrap_admin:
        raise policy.forbidden("email configuration is available to the bootstrap admin only")
    from brains.api.admin_key import ensure_admin_key
    from brains.audit import required_effect
    from brains.config import reload_settings
    from brains.control.secure_settings import set_value

    key, _ = ensure_admin_key(print_banner=False)
    try:
        with required_effect(
            actor="admin",
            action="admin.secure_setting_write",
            payload={"name": name},
        ):
            result = set_value(name, body.value, admin_key=key)
        reload_settings()
        return result
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.delete("/admin/configuration/email/{name}")
def clear_email_configuration(
    name: str,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    if not principal.is_bootstrap_admin:
        raise policy.forbidden("email configuration is available to the bootstrap admin only")
    from brains.api.admin_key import ensure_admin_key
    from brains.audit import required_effect
    from brains.config import reload_settings
    from brains.control.secure_settings import clear_value

    key, _ = ensure_admin_key(print_banner=False)
    try:
        with required_effect(
            actor="admin",
            action="admin.secure_setting_clear",
            payload={"name": name},
        ):
            result = clear_value(name)
        reload_settings()
        return result
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post("/admin/configuration/email/test")
def test_email_configuration(
    body: MailTestBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    if not principal.is_bootstrap_admin:
        raise policy.forbidden("email configuration is available to the bootstrap admin only")
    from brains.control.mailer import MailerError, send_email

    try:
        return send_email(
            body.to,
            "Brains email configuration test",
            "Your Brains SMTP/SES configuration is working.",
        )
    except (MailerError, ValueError) as exc:
        raise _bad_request(exc) from exc


@router.get("/admin/coordination/overview")
def coordination_overview(
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    if not principal.is_bootstrap_admin:
        raise policy.forbidden("coordination overview is available to the bootstrap admin only")
    from brains import service
    from brains.control.claims import list_workspace_claims
    from brains.control.handoffs import list_handoffs
    from brains.control.knowledge import search_knowledge
    from brains.control.patterns import list_patterns
    from brains.control.sessions import list_workspaces
    from brains.control.tasks import list_tasks
    from brains.control.topics import list_topics, live_agent_sessions

    return {
        "live_agents": live_agent_sessions(),
        "workspaces": list_workspaces(),
        "claims": list_workspace_claims(),
        "tasks": list_tasks(limit=100),
        "handoffs": list_handoffs(active_only=True),
        "topics": list_topics(limit=100),
        "patterns": list_patterns(status="all", limit=100),
        "knowledge": search_knowledge(status="active", limit=50),
        "service": service.status(),
    }


class IntegrationDeliveryRecoveryBody(BaseModel):
    attempt: int = Field(ge=1)


@router.get("/config/integrations/deliveries")
def integration_deliveries(
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    if not principal.is_bootstrap_admin:
        raise policy.forbidden("integration delivery recovery is bootstrap-admin only")
    from brains.control import integration_deliveries as deliveries_ctl

    return {"data": deliveries_ctl.list_deliveries(status=status, limit=limit)}


@router.post("/config/integrations/deliveries/{delivery_id}/release")
def release_integration_delivery(
    delivery_id: int,
    body: IntegrationDeliveryRecoveryBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    """Fail a stuck attempt after an operator confirms its worker is no longer active."""
    if not principal.is_bootstrap_admin:
        raise policy.forbidden("integration delivery recovery is bootstrap-admin only")
    from brains.control import integration_deliveries as deliveries_ctl

    try:
        return deliveries_ctl.settle(
            delivery_id,
            "failed",
            attempt=body.attempt,
            detail="released_by_operator",
        )
    except deliveries_ctl.IntegrationDeliveryOwnershipError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/config/providers/{name}/test")
def test_provider(
    name: str,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    """Test connectivity to a configured provider (F7.1) — a real probe that
    lists the provider's models; returns ok/fail without ever raising."""
    if not principal.is_bootstrap_admin:
        raise policy.forbidden("provider tests are available to the bootstrap admin only")
    from brains.admin.service import provider_status_view
    from brains.providers.registry import get_provider

    status = next((row for row in provider_status_view() if row["name"] == name), None)
    if status is None:
        return {
            "ok": False,
            "status": "unknown",
            "stage": "configuration",
            "latency_ms": 0,
            "detail": "Unknown provider.",
        }
    if status["is_stub"]:
        return {
            "ok": False,
            "status": "simulated",
            "stage": "configuration",
            "latency_ms": 0,
            "detail": "This is a simulated provider; no upstream connection exists.",
        }
    if not status["configured"]:
        return {
            "ok": False,
            "status": "unconfigured",
            "stage": "configuration",
            "latency_ms": 0,
            "detail": status["reason"],
        }
    started = time.monotonic()
    try:
        provider = get_provider(name)
        models = provider.list_models() or []
        return {
            "ok": True,
            "status": "reachable",
            "stage": "ok",
            "latency_ms": int((time.monotonic() - started) * 1000),
            "detail": f"{len(models)} models reachable.",
        }
    except Exception:
        return {
            "ok": False,
            "status": "degraded",
            "stage": "connectivity",
            "latency_ms": int((time.monotonic() - started) * 1000),
            "detail": "The configured provider did not complete the bounded connectivity probe.",
        }


class QueueHealthRepairBody(BaseModel):
    apply: bool = False


@router.get("/admin/readiness")
def readiness(principal: Principal = Depends(require_operator_principal)) -> dict:
    """Bootstrap-admin operational-health surface (B8).

    Distinct from ``GET /health``, which stays open and liveness-only: this
    is a protected, redacted readiness contract reporting one overall
    ``ready``/``degraded`` verdict plus bounded per-component state for
    storage/migration access, coordination-queue health, durable mailbox
    delivery/wakeup/SMTP state, and recovery-policy readiness. No component
    ever returns a secret or a raw exception message - only its type name.

    Withdrawn Runtime and live provider state are deliberately NOT part of this contract: a
    simulated/unconfigured model provider is withdrawn routing state, not an
    operational outage, and folding it in here would make every
    lean-core install without a configured provider permanently "degraded"
    for a reason that has nothing to do with whether Brains itself is
    operating correctly.
    """
    if not principal.is_bootstrap_admin:
        raise policy.forbidden("operational readiness is available to the bootstrap admin only")

    from brains.control.operations import readiness_report

    return readiness_report()


@router.get("/admin/queue-health")
def queue_health_status(principal: Principal = Depends(require_operator_principal)) -> dict:
    """Bootstrap-admin coordination-queue health + orphan/stale diagnosis
    (B8): family summary (owner/scope/lifecycle/expiry + counts) plus
    bounded, non-destructive orphan/stale-lease detection. Nothing here
    mutates any row - see ``POST /v1/admin/queue-health/repair`` to act."""
    if not principal.is_bootstrap_admin:
        raise policy.forbidden("queue health is available to the bootstrap admin only")
    from brains.control.queue_health import diagnose, summarize

    return {"summary": summarize(), "diagnosis": diagnose()}


@router.get("/admin/event-scope")
def event_scope_status(principal: Principal = Depends(require_operator_principal)) -> dict:
    """Bootstrap-admin event taxonomy and unresolved-scope posture."""
    if not principal.is_bootstrap_admin:
        raise policy.forbidden("event scope is available to the bootstrap admin only")
    from brains.control.events import event_scope_report

    return event_scope_report()


@router.post("/admin/queue-health/repair")
def queue_health_repair(
    body: QueueHealthRepairBody = Body(default_factory=QueueHealthRepairBody),
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    """Dry-run (default) or apply the objectively-safe continuity repairs
    (B8). ``apply=false`` (default) returns what *would* change,
    mutating nothing; ``apply=true`` performs exactly those actions via each
    family's own existing fenced helper and reports what was actually done.
    Never deletes unresolved work - see ``brains.control.queue_health``."""
    if not principal.is_bootstrap_admin:
        raise policy.forbidden("queue health repair is available to the bootstrap admin only")
    from brains.control.queue_health import apply_repair, plan_repair

    if not body.apply:
        return {"applied": False, **plan_repair()}
    return {"applied": True, **apply_repair()}


@router.get("/admin/recovery-policy")
def recovery_policy_status(principal: Principal = Depends(require_operator_principal)) -> dict:
    """Bootstrap-admin recovery-policy surface (BL-P1-09): the declared
    backup scope/schedule/retention/encryption/ownership/RTO/RPO/offsite/
    drill policy, redacted, plus its completeness and the migration/backup
    compatibility precheck. Never claims backups are "managed" unless every
    mandatory field is configured; never fabricates a schedule or a drill
    date this install did not declare."""
    if not principal.is_bootstrap_admin:
        raise policy.forbidden("recovery policy is available to the bootstrap admin only")
    from brains.control.recovery_policy import recovery_readiness

    return recovery_readiness()


@router.get("/approvals/{code}")
def get_approval(
    code: str,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    _authorized_approval(principal, code, CAP_ORG_READ)
    row = decisions_ctl.get_decision(code)
    if row is None:
        raise policy.not_found("approval", code)
    return row


@router.post("/approvals/{code}/resolve")
def resolve_approval(
    code: str,
    body: ResolveBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    chosen, reasoning, status = _resolve_to_status(body)
    result = _resolve_approval(principal, code, chosen, reasoning, status, body.session_id)
    publish_inbox(None, "approval.resolved", result)
    return result


@router.post("/approvals/{code}/route")
def route_approval(
    code: str,
    body: ApprovalRouteBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    """Assign, prioritize, deadline, or escalate one open approval."""
    _authorized_approval(principal, code, CAP_ORG_WRITE)
    try:
        return decisions_ctl.route_decision(
            code,
            assigned_operator=body.assigned_operator,
            clear_assignment=body.clear_assignment,
            priority=body.priority,
            due_at=body.due_at,
            clear_due=body.clear_due,
            escalation_level=body.escalation_level,
            escalation_reason=body.escalation_reason,
            principal=principal,
        )
    except decisions_ctl.ApprovalAuthorizationError as exc:
        raise policy.forbidden(str(exc)) from exc
    except ValueError as exc:
        if "not open" in str(exc):
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        raise _bad_request(exc) from exc


@router.post("/approvals/{code}/escalate")
def escalate_approval(
    code: str,
    body: ApprovalEscalateBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    """Increment an open approval's escalation level with human attribution."""
    _authorized_approval(principal, code, CAP_ORG_WRITE)
    try:
        return decisions_ctl.escalate_decision(
            code,
            reason=body.reason,
            assigned_operator=body.assigned_operator,
            due_at=body.due_at,
            principal=principal,
        )
    except decisions_ctl.ApprovalAuthorizationError as exc:
        raise policy.forbidden(str(exc)) from exc
    except ValueError as exc:
        if "not open" in str(exc):
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        raise _bad_request(exc) from exc


# --------------------------------------------------------------------------- #
# Sessions (read + spawn)
# --------------------------------------------------------------------------- #


@router.get("/sessions")
def list_sessions(
    status: str | None = None,
    issue_id: int | None = None,
    persona_id: int | None = None,
    runtime_id: int | None = None,
    workspace_id: int | None = None,
    machine_id: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    rows = sessions_ctl.list_agent_sessions(
        status=status,
        issue_id=issue_id,
        persona_id=persona_id,
        runtime_id=runtime_id,
        workspace_id=workspace_id,
        machine_id=machine_id,
    )
    return paginate(_scope_sessions(principal, rows), limit=limit, cursor=cursor)


@router.post("/sessions/spawn")
def spawn_session(
    body: SpawnBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    """Enqueue a spawn order.

    Every identifier in the body is authorized independently: the spawn binds
    the Persona to the Issue and rewrites the Persona's Runtime, so authorizing
    only one of them would let a principal that owns a Persona in its own Org
    reassign another Org's Issue and re-point it at another Org's Runtime.

    A Runtime row with no Org is a pre-Org legacy registration and makes no Org
    claim, so it neither grants nor blocks; enrollment always binds one.
    """
    from brains.control import runtimes as runtimes_ctl

    named = 0
    scoped: list[tuple[str, object, int | None]] = []
    if body.persona_id is not None:
        named += 1
        scoped.append(("persona", body.persona_id, policy.persona_org_id(body.persona_id)))
    if body.issue_id is not None:
        named += 1
        scoped.append(("issue", body.issue_id, policy.issue_org_id(body.issue_id)))
    if body.runtime_id is not None:
        named += 1
        rt = runtimes_ctl.get_runtime(body.runtime_id)
        if rt is None:
            raise policy.not_found("runtime", body.runtime_id)
        if rt.get("org_id") is not None:
            scoped.append(("runtime", body.runtime_id, rt["org_id"]))
    if not named:
        raise _bad_request(
            ValueError("a spawn must name a persona, an issue, or a runtime to scope it")
        )
    if not scoped:
        raise policy.not_found("runtime", body.runtime_id)
    for entity, ref, org_id in scoped:
        if org_id is None:
            raise policy.not_found(entity, ref)
        policy.require_capability(principal, CAP_ORG_WRITE, org_id, entity=entity, ref=ref)
    org_ids = {org_id for _entity, _ref, org_id in scoped}
    if len(org_ids) > 1:
        # A spawn links its targets together, so it cannot straddle two Orgs.
        entity, ref, _org = scoped[-1]
        raise policy.not_found(entity, ref)
    try:
        result = assignments_ctl.enqueue_spawn(
            issue_id=body.issue_id,
            persona_id=body.persona_id,
            runtime_id=body.runtime_id,
            prompt=body.prompt,
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc
    publish_session(None, "session.started", result)
    return result


@router.get("/sessions/{session_id}")
def get_session(
    session_id: str,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    return _authorized_session(principal, session_id, CAP_ORG_READ)


class SessionStateBody(BaseModel):
    state: str
    summary: str | None = None


def _session_runtime_machine_id(row: dict) -> str | None:
    """The machine a Session's agent process actually runs on.

    The Session's own ``machine_id`` records where the *row* was created, and
    for a spawn that is the hub. Where the Session is bound to a Runtime, that
    Runtime's registered machine is the fact; the stamp is the fallback for a
    Session no Runtime ever claimed.
    """
    from brains.control import runtimes as runtimes_ctl

    runtime_id = row.get("runtime_id")
    if runtime_id is not None:
        runtime = runtimes_ctl.get_runtime(runtime_id)
        if runtime and runtime.get("machine_id"):
            return str(runtime["machine_id"])
    return row.get("machine_id")


@router.post("/sessions/{session_id}/state")
def set_session_state(
    session_id: str,
    body: SessionStateBody,
    principal: Principal = Depends(require_console_principal),
) -> dict:
    """Transition a session's explicit lifecycle state (F3.2): spawning ->
    running -> blocked | completed | failed. The daemon/agent calls this so the
    console reflects status live (terminal states stamp a duration).

    A Runtime credential may report state for a Session bound to it - that is
    ``runtime.execute`` - and for nothing else. The binding is read from the
    Session's Runtime rather than from the Session's machine stamp: a spawn
    row is created by the hub, so it carries the hub's machine until the
    daemon opens it, and authorizing against the stamp would refuse the very
    Runtime that ran the agent - leaving the Session ``running`` forever after
    its process ended. Both bindings are still checked, not only the machine:
    the Session's Runtime and Workspace must also sit in the Org the
    credential was minted for, so a credential that somehow named another
    Org's machine could still not write into that Org's live console. A
    Session whose Workspace declares no Org, or declares the ``default``
    bucket a Workspace created on the fly lands in, claims nothing and is left
    to the machine binding alone.
    """
    if principal.is_runtime:
        row = sessions_ctl.get_agent_session(session_id)
        if row is None:
            raise policy.not_found("session", session_id)
        machine_id = _session_runtime_machine_id(row)
        if not machine_id:
            # A Session that names no machine is not running on this one.
            raise policy.not_found("session", session_id)
        policy.authorize_runtime_operation(
            principal,
            "runtime.execute",
            machine_id=machine_id,
            org_id=policy.runtime_declared_org_id(row.get("runtime_id")),
        )
        session_org = policy.workspace_declared_org_id(row.get("workspace_id"))
        if (
            session_org is not None
            and session_org != policy.default_org_id()
            and session_org != principal.runtime_org_id
        ):
            raise policy.not_found("session", session_id)
    else:
        _authorized_session(principal, session_id, CAP_ORG_WRITE)
    try:
        result = sessions_ctl.set_session_state(session_id, body.state, summary=body.summary)
    except sessions_ctl.AgentSessionNotFoundError as exc:
        raise policy.not_found("session", session_id) from exc
    except ValueError as exc:
        raise _bad_request(exc) from exc
    return result


@router.get("/sessions/{session_id}/events")
def session_events(
    session_id: str,
    limit: int | None = None,
    cursor: str | None = None,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    _authorized_session(principal, session_id, CAP_ORG_READ)
    rows = sessions_ctl.list_agent_session_events(session_id)
    return paginate(rows, limit=limit, cursor=cursor)


# --------------------------------------------------------------------------- #
# Session control (durable message + stop, BL-P0-05)
# --------------------------------------------------------------------------- #


class SessionMessageBody(BaseModel):
    text: str
    #: The caller's idempotency handle. The console mints one per composer
    #: submit and re-sends it on retry, so a replayed request is the same
    #: logical message rather than a second prompt.
    operation_id: str | None = None


class SessionStopBody(BaseModel):
    operation_id: str | None = None
    reason: str | None = None


def _requested_by(principal: Principal) -> str:
    """Who asked, for the durable record.

    Attribution is taken from the resolved principal, never from the body: a
    caller that could name its own requester could file a command in somebody
    else's name.
    """
    return principal.describe()[:128]


def _enqueue_command(
    principal: Principal,
    session_id: str,
    kind: str,
    *,
    text: str | None = None,
    reason: str | None = None,
    operation_id: str | None = None,
) -> dict:
    """Authorize, record, then attempt delivery - in that order.

    The row commits before anything is announced or delivered, so a reload
    after a crashed request shows the command that was accepted rather than
    nothing at all. Delivery is attempted afterwards and only for a Session
    whose agent process *this* process owns; anything else is left for the
    Runtime that does own it to claim.
    """
    _authorized_session(principal, session_id, CAP_ORG_WRITE)
    try:
        command, created = commands_ctl.enqueue(
            session_id,
            kind,
            text=text,
            reason=reason,
            operation_id=operation_id,
            requested_by=_requested_by(principal),
        )
    except commands_ctl.UnknownSessionError as exc:
        raise policy.not_found("session", session_id) from exc
    except commands_ctl.SessionCommandError as exc:
        raise _bad_request(exc) from exc
    if created and command["status"] == commands_ctl.STATUS_REQUESTED:
        command = _dispatch_locally(command)
    return {**command, "duplicate": not created}


def _dispatch_locally(command: dict) -> dict:
    """Deliver a command whose agent process this process owns, if any.

    A Session launched by the hub process itself (the streamed console
    session) has no remote Runtime to claim its commands, so it would sit in
    ``requested`` forever. Delivery still goes through the queue - claim,
    execute, acknowledge - so the local path and the daemon path produce the
    same durable record.
    """
    try:
        from brains.exec import session_dispatch

        settled = session_dispatch.dispatch_owned(session_id=command["session_id"])
    except Exception:  # pragma: no cover - local delivery is best effort
        return command
    for row in settled:
        if row.get("command_id") == command.get("command_id"):
            return row
    return commands_ctl.get(command["command_id"]) or command


@router.post("/sessions/{session_id}/message")
def message_session(
    session_id: str,
    body: SessionMessageBody,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    """Queue an operator message for a running Session.

    The message is durable before it is delivered and is delivered at most
    once: a retry carrying the same ``operation_id`` returns the original
    command with ``duplicate: true`` instead of queueing a second prompt.

    Where the Session's agent has no input channel - which is the case for
    every CLI Brains launches in its non-interactive shape - the command is
    recorded and settled ``failed``/``unsupported`` with the reason, because
    reporting a message as sent to a process that cannot receive it is the
    fabrication this route exists to remove.
    """
    return _enqueue_command(
        principal,
        session_id,
        commands_ctl.KIND_MESSAGE,
        text=body.text,
        operation_id=body.operation_id,
    )


@router.post("/sessions/{session_id}/stop")
def stop_session(
    session_id: str,
    body: SessionStopBody | None = None,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    """Request that a Session's agent process be stopped.

    Idempotent by construction: with no explicit ``operation_id`` the command
    keys on the Session, so a second press returns the first command rather
    than sending a second signal. A Session that has already finished answers
    with an acknowledged ``already_terminal`` command, so a stop racing a
    natural completion is truthful rather than an error.

    The Session is only recorded as ended when a consumer proves the process
    is gone; a Runtime that no longer owns the process answers ``not_owned``
    and the Session is reconciled instead of being marked stopped on a hope.
    """
    payload = body or SessionStopBody()
    return _enqueue_command(
        principal,
        session_id,
        commands_ctl.KIND_STOP,
        reason=payload.reason,
        operation_id=payload.operation_id,
    )


@router.get("/sessions/{session_id}/commands")
def session_commands(
    session_id: str,
    limit: int | None = None,
    cursor: str | None = None,
    principal: Principal = Depends(require_operator_principal),
) -> dict:
    """The durable message/stop history for a Session, oldest first.

    This is what makes a console reload show what was actually asked for and
    what became of it, rather than whatever the previous page load happened to
    hold in memory.
    """
    _authorized_session(principal, session_id, CAP_ORG_READ)
    return paginate(commands_ctl.list_for_session(session_id), limit=limit, cursor=cursor)
