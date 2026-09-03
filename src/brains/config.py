from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from brains.extras import ExtraNotInstalledError


class ModelRoute(BaseModel):
    provider: str
    model: str


class ControlPolicy(BaseModel):
    require_approval_for_deep: bool = False
    require_approval_for_external_docs: bool = False
    require_approval_for_large_scans: bool = False
    generated_views_dir: str = ".brains/views"


class RouterConfig(BaseModel):
    """Top-level router behaviour.

    ``enabled=True`` (the default) keeps tier-routing available:
    clients can opt into classifier-driven routing by sending
    ``model=brains/auto`` (the legacy ``brains-auto`` is also
    accepted). Everything else — explicit model ids, tier aliases
    (``brains/cheap``, ``brains/default``, etc.) — is honoured
    verbatim with NO classifier involvement. Unknown model ids return
    404; the classifier never silently reroutes a request the client
    didn't ask it to.

    ``enabled=False`` puts the gateway in **pure-proxy mode**: the
    classifier-driven ``brains/auto`` alias is disabled (returns 404).
    Explicit model ids and pinned tier aliases still work. This is the
    "really stay out of the way" mode for operators who want zero
    chance of classifier influence.

    ``expose_classifier_in_response`` controls whether the
    OpenAI-shaped response carries a non-standard ``brains`` block
    with the classification + plan that produced the route. Off by
    default — it's debug data for the operator, not contract for the
    client, and shipping it on the wire was inconsistent with the
    Anthropic facade (which never carried it). Operators who want it
    back (e.g. for dashboards that read ``response.brains.classification``)
    can flip the flag in the overlay.
    """

    enabled: bool = Field(default=True)
    expose_classifier_in_response: bool = Field(default=False)


class SavingsConfig(BaseModel):
    """Cost-savings ledger configuration.

    The ledger records, for every successful gateway call, the routed
    model + token counts + the actual cost computed from the static
    price catalog. It also records a *baseline* cost — what the same
    call would have cost on the tier the user would have used without
    brains in front. The difference is reported as "savings" on the
    dashboard.

    ``baseline_model`` is the model to use as the "what it would have
    cost without brains" reference. Empty string (the default) means:
    fall back to the ``default`` tier's model — i.e. compare every
    routed call against the model the operator marked as "default".
    Operators who run brains primarily to keep frontier models from
    being used on trivial work should set this to whatever flagship
    they'd otherwise reach for (``"claude-opus-4"``, ``"gpt-4o"``,
    etc.) so the dashboard headline reflects that intent.

    ``price_catalog`` is an overlay over the built-in
    :data:`brains.router.prices.DEFAULT_PRICES` map. Shape is
    ``{model_id: {input: <usd-per-1M>, output: <usd-per-1M>}}``.
    Anything in here wins over the static catalog; longest-prefix
    match still applies, so you can override a whole family with one
    entry.
    """

    enabled: bool = Field(default=True)
    baseline_model: str = Field(default="")
    price_catalog: dict[str, dict[str, float]] = Field(default_factory=dict)


class ProviderPolicyConfig(BaseModel):
    """Per-provider retry + circuit-breaker policy.

    All fields are off-by-default so an empty entry (or a missing entry
    altogether) preserves the historical "one attempt, no breaker"
    behaviour. See :mod:`brains.providers.policy` for the runtime
    implementation.
    """

    retry_max_attempts: int = Field(default=1, ge=1, le=10)
    retry_initial_backoff_seconds: float = Field(default=0.5, ge=0.0)
    retry_max_backoff_seconds: float = Field(default=8.0, ge=0.0)
    retry_backoff_multiplier: float = Field(default=2.0, ge=1.0)
    circuit_failure_threshold: int = Field(default=0, ge=0)
    circuit_cooldown_seconds: float = Field(default=30.0, ge=0.0)


# --- Optional runtime subsystems ---
#
# Each block is OFF by default. Setting ``enabled: true`` requires the
# matching pip extra to be installed; if it's not, :func:`load_settings`
# raises :class:`brains.extras.ExtraNotInstalledError` at startup with the
# exact ``pip install`` command.


class StorageConfig(BaseModel):
    """Selects the storage backend. ``sqlite`` is the lean-core default."""

    backend: str = Field(default="sqlite")  # sqlite | postgres


class BridgeConfig(BaseModel):
    """Shared shape for every operator-control bridge plugin."""

    enabled: bool = Field(default=False)


class BridgesConfig(BaseModel):
    telegram: BridgeConfig = Field(default_factory=BridgeConfig)
    slack: BridgeConfig = Field(default_factory=BridgeConfig)
    whatsapp: BridgeConfig = Field(default_factory=BridgeConfig)
    whatsapp_web: BridgeConfig = Field(default_factory=BridgeConfig)


class TelemetryConfig(BaseModel):
    """OpenTelemetry export config.

    ``endpoint`` and ``service_name`` are optional config fallbacks. The
    standard ``OTEL_EXPORTER_OTLP_ENDPOINT`` / ``OTEL_SERVICE_NAME``
    environment variables, when set, take precedence so deployments can
    point at a collector without touching the overlay.
    """

    enabled: bool = Field(default=False)
    endpoint: str | None = Field(default=None)
    service_name: str = Field(default="brains")


class SubsystemsConfig(BaseModel):
    """All optional runtime subsystems gated by pip extras."""

    storage: StorageConfig = Field(default_factory=StorageConfig)
    bridges: BridgesConfig = Field(default_factory=BridgesConfig)
    otel: TelemetryConfig = Field(default_factory=TelemetryConfig)


# Maps an enabled-subsystem path → the pip extra that must be installed.
# The exact (subsystem-path, extra-name) pairs are surfaced in error
# messages so the operator gets a one-shot ``pip install`` hint.
_SUBSYSTEM_EXTRA_GATES: tuple[tuple[str, str], ...] = (
    ("subsystems.bridges.telegram", "telegram"),
    ("subsystems.bridges.slack", "slack"),
    ("subsystems.bridges.whatsapp", "whatsapp"),
    ("subsystems.bridges.whatsapp_web", "whatsapp_web"),
    ("subsystems.otel", "otel"),
)


# Default location for admin-managed overlay edits. Lives alongside the
# baked config so admin edits never overwrite the operator's hand-authored
# BRAINS_CONFIG. Resolved relative to CWD unless absolute.
DEFAULT_RUNTIME_OVERLAY = "brains.runtime.yaml"

# Schema version embedded in ``brains.runtime.yaml``. Bumped whenever the
# overlay's expected shape changes in a non-backwards-compatible way so we
# can migrate older files instead of silently ignoring fields.
RUNTIME_OVERLAY_SCHEMA_VERSION = 1


# Literal ``db_url`` value that means "use the central per-machine brain
# DB". Kept as a constant so the validator below and any external caller
# (tests, wire helpers) refer to the same sentinel string.
DEFAULT_DB_URL = "sqlite:///brains.db"


def _canonical_default_db_url() -> str:
    """Resolve the bare ``sqlite:///brains.db`` default to an absolute URL.

    Brains is a single-machine coordination plane: every CLI invocation,
    every long-running server, and every stdio MCP child must share one
    DB or sessions silently fragment into per-CWD ``brains.db`` files.
    The original default is CWD-relative, so this helper rewrites it to
    ``~/.brains/brains.db`` (honouring ``BRAINS_STATE_DIR`` when set).

    Mirrors the path resolution in ``brains.cli.app._canonical_db_url``
    and ``brains.api.admin_key.state_dir`` so all three agree on where
    the shared brain lives.
    """
    state = os.environ.get("BRAINS_STATE_DIR")
    base = Path(state).expanduser() if state else (Path.home() / ".brains")
    return "sqlite:///" + (base / "brains.db").as_posix()


def brains_state_dir() -> Path:
    """The per-machine brains state directory (``~/.brains`` by default,
    or ``BRAINS_STATE_DIR`` when set). Shared by the DB, admin key, and the
    local secrets fallback so they all agree on one location."""
    state = os.environ.get("BRAINS_STATE_DIR")
    return Path(state).expanduser() if state else (Path.home() / ".brains")


def secrets_env_path() -> Path:
    """Local, gitignored fallback secrets file (``<state_dir>/secrets.env``).

    The admin Environment page can persist a value here when the operator
    explicitly opts in. It is merged into ``os.environ`` at settings-load
    time so ``${ENV:NAME}`` references resolve after a restart. The real
    process environment always wins — entries here are only applied when
    the name is otherwise unset, and brains never writes secrets to YAML.
    """
    return brains_state_dir() / "secrets.env"


def load_secrets_env() -> None:
    """Merge ``<state_dir>/secrets.env`` (``KEY=VALUE`` lines) into
    ``os.environ`` without clobbering values already present in the real
    environment."""
    path = secrets_env_path()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if name and name not in os.environ:
            os.environ[name] = value.strip()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BRAINS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    db_url: str = Field(default=DEFAULT_DB_URL)

    # SQLite foreign-key enforcement is OFF by default because existing
    # stores can still hold violations from before the integrity work
    # (BL-P0-07); switching it on blindly turns latent damage into
    # unpredictable write failures. When enabled, the connect hook proves
    # ``PRAGMA foreign_key_check`` is clean once per database and fails
    # loudly otherwise — see ``brains.storage.integrity``.
    sqlite_enforce_foreign_keys: bool = Field(default=False)
    # One SQLite writer serves every gateway, MCP, CLI, and agent process.
    # Wait through ordinary multi-session bursts instead of misreporting them
    # as outages; a genuinely stuck transaction is still surfaced after the
    # bounded wait and handled through the recovery tooling.
    sqlite_busy_timeout_ms: int = Field(default=30_000, ge=0, le=300_000)

    @field_validator("db_url", mode="after")
    @classmethod
    def _rewrite_bare_default_db_url(cls, value: str) -> str:
        """Translate the historical bare default to the shared per-machine DB.

        Any explicit override (absolute path, postgres URL, etc.) is
        returned untouched. Only the literal sentinel
        ``sqlite:///brains.db`` is rewritten — that string is the
        documented "I didn't choose, use the default" signal and was
        previously interpreted as CWD-relative, which silently created
        a fresh per-directory brain on every bare-CLI invocation.
        """
        if value == DEFAULT_DB_URL:
            return _canonical_default_db_url()
        return value

    # Empty by default. The effective key is resolved at startup by
    # brains.api.admin_key.ensure_admin_key(), which honors BRAINS_API_KEY,
    # falls back to ~/.brains/admin-key, and otherwise auto-generates one.
    api_key: str = Field(default="")
    allow_unauthenticated_api: bool = Field(default=False)
    # Opt-in server-side enforcement: when non-empty, this system message is
    # prepended to every gateway model call (Phase 3 / strict). The agent
    # composes its request first, so it cannot strip this preamble — it is
    # added after, on the way to the provider. Empty = disabled (default).
    gateway_preamble: str = Field(default="")
    config: str | None = Field(default=None)
    runtime_overlay: str = Field(default=DEFAULT_RUNTIME_OVERLAY)
    workspace: str | None = Field(default=None)
    docs_stale_days: int = Field(default=45)
    repo_index_max_file_size: int = Field(default=512_000)
    # Request-trace retention. write_trace() stores a redacted copy of each
    # gateway request for the admin "Recent traces" view. Left unbounded the
    # traces table dominates the DB (full message/context JSON per call), so:
    #   * payloads larger than ``trace_max_payload_bytes`` are truncated
    #     (0 disables truncation);
    #   * only the most recent ``trace_retention_max_rows`` rows are kept
    #     (0 disables pruning / retains everything).
    trace_max_payload_bytes: int = Field(default=16_384, ge=0)
    trace_retention_max_rows: int = Field(default=2_000, ge=0)
    context_compression_enabled: bool = Field(default=False)
    savings_holdout_fraction: float = Field(default=0.0, ge=0.0, le=1.0)
    freshness_timeout_seconds: float = Field(default=5.0)
    ollama_base_url: str = Field(default="http://127.0.0.1:11434")
    ollama_timeout_seconds: float = Field(default=30.0)
    # Embedding model for the semantic retrieval layer (Ollama /api/embed).
    # Empty = embeddings disabled. e.g. "nomic-embed-text".
    embed_model: str = Field(default="")
    # Discovery auto-bootstrap. Make semantic search + the code graph "just work"
    # on first use instead of returning a silent empty until someone runs
    # `embed-repo` / `graph-build` by hand. Indexing is LOCAL — AST parsing for the
    # graph (no network), Ollama for embeddings (a local model, NOT paid LLM
    # tokens) — so it spends free local compute + a one-time latency to SAVE the
    # paid grep/read turns an un-indexed agent would otherwise burn. Both are
    # idempotent (content-hash / rebuild-on-empty), so only the first call pays.
    semantic_auto_embed: bool = Field(default=True)
    graph_auto_build: bool = Field(default=True)
    # Background pre-index on session start: build the graph + embed the repo in a
    # daemon thread when a session opens, so even the FIRST retrieval is instant
    # instead of paying the cold-index latency. Best-effort, deduped, idempotent
    # (only works when not already indexed), never blocks session start.
    prewarm_index_on_session: bool = Field(default=True)
    # Bound first-call latency: skip inline auto-embed when a repo has more than
    # this many candidate files (the caller still gets a hint to embed-repo).
    auto_index_max_files: int = Field(default=1500, ge=0)
    openai_compatible_base_url: str = Field(default="https://api.openai.com/v1")
    openai_compatible_api_key: str | None = Field(default=None)
    openai_compatible_timeout_seconds: float = Field(default=60.0)
    litellm_timeout_seconds: float = Field(default=60.0)
    # Outbound email (SMTP; SES works through its SMTP endpoint = config only).
    # Empty smtp_host = mailer disabled. Password may be a "${ENV_NAME}" ref
    # resolved from the environment / secrets.env like provider keys.
    smtp_host: str = Field(default="")
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_username: str = Field(default="")
    smtp_password: str = Field(default="")  # env-ref friendly, never logged
    smtp_from: str = Field(default="")  # e.g. "Brains <brains@example.com>"
    smtp_use_starttls: bool = Field(default=True)
    smtp_timeout_seconds: float = Field(default=15.0)
    # Operator notify address: ASKs are emailed here when the mailer is on.
    operator_notify_email: str = Field(default="")
    # GitHub Copilot provider (proxies api.githubcopilot.com via an
    # OAuth-resolved session token). See ``brains.auth.copilot`` for the
    # auth flow. ``oauth_token`` is the env override and stays out of the
    # admin overlay so secrets never live on disk; the cache file does.
    github_copilot_oauth_token: str = Field(default="")
    github_copilot_use_gh_cli: bool = Field(default=True)
    github_copilot_cache_dir: str = Field(default="")
    github_copilot_timeout_seconds: float = Field(default=60.0)
    github_copilot_editor_version: str = Field(default="vscode/1.95.3")
    github_copilot_integration_id: str = Field(default="vscode-chat")
    github_webhook_secret: str = Field(default="")
    github_repository_org_bindings: tuple[str, ...] = Field(default=())
    # Safety gate for the github_copilot provider. Default-OFF: the provider
    # impersonates an editor OAuth app against a per-machine token cache and is
    # a personal-use grey area (docs/OPERATIONS.md), so it must be explicitly
    # enabled (here or via BRAINS_EXPERIMENTAL_COPILOT_PROVIDER=1) and is refused
    # in shared contexts (multi-operator / Postgres) where one operator would
    # borrow another's Copilot entitlement.
    allow_copilot_proxy: bool = Field(default=False)
    rate_limit_per_minute: int = Field(default=0)
    api_keys: tuple[str, ...] = Field(default=())
    source_allowlist: tuple[str, ...] = Field(default=())
    control: ControlPolicy = Field(default_factory=ControlPolicy)
    router: RouterConfig = Field(default_factory=RouterConfig)
    savings: SavingsConfig = Field(default_factory=SavingsConfig)
    subsystems: SubsystemsConfig = Field(default_factory=SubsystemsConfig)
    redaction_sensitive_key_parts: tuple[str, ...] = Field(
        default=(
            "api_key",
            "apikey",
            "token",
            "secret",
            "password",
            "authorization",
        )
    )
    models: dict[str, ModelRoute] = Field(
        default_factory=lambda: {
            "cheap": ModelRoute(provider="echo", model="echo-small"),
            "default": ModelRoute(provider="echo", model="echo-default"),
            "strong": ModelRoute(provider="echo", model="echo-strong"),
            "deep": ModelRoute(provider="echo", model="echo-deep"),
        }
    )
    routes: dict[str, str] = Field(
        default_factory=lambda: {
            "trivial": "cheap",
            "code_fix": "default",
            "architecture": "strong",
            "research": "strong",
            "docs_lookup": "default",
            "unknown": "cheap",
        }
    )
    provider_policies: dict[str, ProviderPolicyConfig] = Field(default_factory=dict)

    # --- Managed recovery policy ---
    #
    # Declares WHAT the operator has committed to for backup/restore, so
    # readiness (B8) and the recovery-policy surfaces can report completeness
    # honestly instead of fabricating a schedule. Everything here is a policy
    # *declaration*, not an enforcement mechanism — brains does not run a
    # scheduler that fires backups on this cadence; an external cron/task
    # scheduler invoking ``brains-ai backup`` is expected to match it. Empty
    # string / zero / False are the "not yet configured" sentinels, never a
    # fabricated default that would make an unconfigured install look ready.
    # No field here may hold a secret or an identity-bearing value (that
    # belongs in the offsite storage's own credential store) — only pointers
    # (an owner name, a location description) that are safe to read back over
    # the admin API and CLI.
    backup_scope: str = Field(default="")
    backup_schedule: str = Field(default="")  # e.g. "daily", "every:6h"; "" = unset
    backup_retention_days: int = Field(default=0, ge=0)  # 0 = unset
    backup_encryption_at_rest: bool = Field(default=False)
    backup_encryption_owner: str = Field(default="")  # who holds the key, not the key itself
    backup_offsite_owner: str = Field(default="")
    backup_offsite_location: str = Field(default="")  # a pointer/description, not a credential
    backup_rto_minutes: int = Field(default=0, ge=0)  # 0 = unset
    backup_rpo_minutes: int = Field(default=0, ge=0)  # 0 = unset
    backup_restore_drill_required: bool = Field(default=True)
    # Local manifest archive selected for recovery. The path itself is never
    # returned by readiness; only its bounded verification result is exposed.
    backup_candidate_path: str = Field(default="")
    # Legacy operator declaration retained for configuration compatibility.
    # Readiness trusts only the audit record produced by ``recovery-drill``.
    backup_last_restore_drill_at: str = Field(default="")


# Keys that the admin UI is allowed to mutate via the runtime overlay.
# Anything outside this allowlist is ignored when reading the overlay so
# operators retain control over sensitive defaults.
ADMIN_EDITABLE_KEYS = frozenset(
    {
        "models",
        "routes",
        "control",
        "router",
        "savings",
        "subsystems",
        "rate_limit_per_minute",
        "ollama_base_url",
        "gateway_preamble",
        "embed_model",
        "ollama_timeout_seconds",
        "openai_compatible_base_url",
        "openai_compatible_api_key",
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
    }
)


# Top-level overlay fields where ``${ENV:NAME}`` references are honored.
# Every other field treats the literal ``${ENV:...}`` string as a plain
# value, so a hostile admin can't smuggle a secret out via, say,
# ``models.default.model: "${ENV:BRAINS_API_KEY}"``. The write-side
# validators in ``brains.admin.service`` reject the syntax up front for
# fields outside this set.
ENV_REF_ALLOWED_FIELDS = frozenset({"openai_compatible_api_key"})

_SECURE_SETTING_FIELDS = (
    "smtp_host",
    "smtp_port",
    "smtp_username",
    "smtp_password",
    "smtp_from",
    "smtp_use_starttls",
    "smtp_timeout_seconds",
    "operator_notify_email",
)


_ENV_REF_PATTERN = re.compile(r"^\$\{ENV:([A-Z][A-Z0-9_]*)\}$")


def _looks_like_env_ref(value: Any) -> bool:
    """Cheap structural check used by validators to spot ``${ENV:...}`` syntax."""
    return isinstance(value, str) and bool(_ENV_REF_PATTERN.match(value.strip()))


def _resolve_env_refs(value: Any) -> Any:
    """Recursively resolve ``${ENV:NAME}`` references against ``os.environ``.

    This is how the admin UI handles secrets: instead of persisting an API
    key in the overlay YAML, the operator stores a reference to an
    environment variable name. The reference is resolved at config-load
    time, so values appear as plain strings to the rest of the system but
    the secret never lives on disk.
    """
    if isinstance(value, str):
        match = _ENV_REF_PATTERN.match(value.strip())
        if match:
            name = match.group(1)
            return os.environ.get(name)
        return value
    if isinstance(value, dict):
        return {k: _resolve_env_refs(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env_refs(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_resolve_env_refs(item) for item in value)
    return value


def _coerce_routes(value: dict[str, Any] | None) -> dict[str, str] | None:
    if not value:
        return None
    routes: dict[str, str] = {}
    for task_type, route in value.items():
        if isinstance(route, str):
            routes[task_type] = route
        elif isinstance(route, dict) and "model_tier" in route:
            routes[task_type] = str(route["model_tier"])
    return routes


def _load_yaml_config(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Brains config not found: {path}")
    with config_path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise ValueError("Brains config must be a YAML mapping")
    return payload


def _load_overlay(path: str | None) -> dict[str, Any]:
    """Read the runtime overlay file, returning an empty dict if absent.

    The overlay carries a ``schema_version`` field. Today there's exactly
    one supported version (:data:`RUNTIME_OVERLAY_SCHEMA_VERSION`). Unknown
    versions are rejected so we never silently misread a future shape.
    Missing version is treated as the current version for backwards
    compatibility with files written before this field existed.
    """
    if not path:
        return {}
    overlay_path = Path(path)
    if not overlay_path.exists():
        return {}
    try:
        with overlay_path.open("r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(payload, dict):
        return {}
    declared_version = payload.get("schema_version", RUNTIME_OVERLAY_SCHEMA_VERSION)
    if declared_version != RUNTIME_OVERLAY_SCHEMA_VERSION:
        raise ValueError(
            f"brains.runtime.yaml schema_version={declared_version!r} is not supported. "
            f"This build expects schema_version={RUNTIME_OVERLAY_SCHEMA_VERSION}."
        )
    return {k: v for k, v in payload.items() if k in ADMIN_EDITABLE_KEYS}


def _apply_payload(base: Settings, payload: dict[str, Any]) -> Settings:
    if not payload:
        return base
    # Resolve ``${ENV:NAME}`` only at the documented allow-listed top-level
    # fields. Every other field is treated as a literal value, so an
    # overlay can't leak environment variables via, e.g., a model name.
    resolved: dict[str, Any] = {}
    for key, value in payload.items():
        if key in ENV_REF_ALLOWED_FIELDS:
            resolved[key] = _resolve_env_refs(value)
        else:
            resolved[key] = value
    updates: dict[str, Any] = {}
    if "models" in resolved:
        updates["models"] = {
            tier: ModelRoute.model_validate(route)
            for tier, route in (resolved.get("models") or {}).items()
        }
    routes = _coerce_routes(resolved.get("routes"))
    if routes is not None:
        updates["routes"] = routes
    if "control" in resolved:
        updates["control"] = ControlPolicy.model_validate(resolved["control"])
    if "router" in resolved:
        updates["router"] = RouterConfig.model_validate(resolved["router"])
    if "savings" in resolved:
        updates["savings"] = SavingsConfig.model_validate(resolved["savings"])
    if "subsystems" in resolved:
        updates["subsystems"] = SubsystemsConfig.model_validate(resolved["subsystems"])
    for key in (
        "db_url",
        "api_key",
        "allow_unauthenticated_api",
        "gateway_preamble",
        "workspace",
        "docs_stale_days",
        "repo_index_max_file_size",
        "trace_max_payload_bytes",
        "trace_retention_max_rows",
        "freshness_timeout_seconds",
        "ollama_base_url",
        "ollama_timeout_seconds",
        "embed_model",
        "openai_compatible_base_url",
        "openai_compatible_api_key",
        "openai_compatible_timeout_seconds",
        "litellm_timeout_seconds",
        "rate_limit_per_minute",
        "api_keys",
        "source_allowlist",
        "github_copilot_use_gh_cli",
        "github_copilot_cache_dir",
        "github_copilot_timeout_seconds",
        "github_copilot_editor_version",
        "github_copilot_integration_id",
        "allow_copilot_proxy",
    ):
        if key in resolved:
            updates[key] = resolved[key]
    return base.model_copy(update=updates)


def _enforce_subsystem_extras(settings_obj: Settings) -> None:
    """Fail closed when historical config attempts withdrawn activation."""
    subs = settings_obj.subsystems
    if subs.storage.backend != "sqlite" or not settings_obj.db_url.startswith("sqlite:"):
        raise ValueError("Postgres runtime storage is withdrawn; SQLite is required")
    bridges = subs.bridges
    withdrawn = (
        ("subsystems.bridges.telegram", bridges.telegram.enabled),
        ("subsystems.bridges.slack", bridges.slack.enabled),
        ("subsystems.bridges.whatsapp", bridges.whatsapp.enabled),
        (
            "subsystems.bridges.whatsapp_web",
            getattr(getattr(bridges, "whatsapp_web", None), "enabled", False),
        ),
        ("subsystems.otel", subs.otel.enabled),
    )
    for path, is_on in withdrawn:
        if is_on:
            raise ValueError(f"{path} is withdrawn and cannot be enabled")


def load_settings() -> Settings:
    load_secrets_env()
    base = Settings()
    base = _apply_payload(base, _load_yaml_config(base.config))
    base = _apply_payload(base, _load_overlay(base.runtime_overlay))
    _enforce_subsystem_extras(base)
    return base


def reload_settings() -> Settings:
    """Re-read overlay + baked config and refresh the module-level singleton.

    Fields on the existing ``settings`` object are mutated in place so every
    ``from brains.config import settings`` import (e.g. in auth, providers,
    router) keeps the same reference and immediately observes the change.

    After the field reset, :func:`brains.api.admin_key.ensure_admin_key` is
    invoked (banner-less, side-effect-free when the key file exists) so the
    runtime-resolved API key — which lives outside the overlay — is
    re-populated. Without this, an admin overlay write would silently lock
    every subsequent request out with "API key not configured".
    """
    fresh = load_settings()
    for field_name in type(fresh).model_fields:
        object.__setattr__(settings, field_name, getattr(fresh, field_name))
    # Local import: ``brains.api.admin_key`` imports ``settings`` from this
    # module, so the dependency must stay one-way at import time.
    from brains.api.admin_key import ensure_admin_key

    ensure_admin_key(print_banner=False)
    return settings


settings = load_settings()


__all__ = [
    "ADMIN_EDITABLE_KEYS",
    "BridgeConfig",
    "BridgesConfig",
    "ControlPolicy",
    "DEFAULT_DB_URL",
    "ExtraNotInstalledError",
    "ModelRoute",
    "RouterConfig",
    "RUNTIME_OVERLAY_SCHEMA_VERSION",
    "Settings",
    "StorageConfig",
    "SubsystemsConfig",
    "TelemetryConfig",
    "load_settings",
    "reload_settings",
    "settings",
]
