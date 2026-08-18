from brains.providers.base import Provider
from brains.providers.echo import EchoProvider
from brains.providers.github_copilot import GitHubCopilotProvider
from brains.providers.litellm_provider import LiteLLMProvider
from brains.providers.ollama import OllamaProvider
from brains.providers.openai_compatible import (
    OpenAICompatibleProvider,
    ProviderInvocationError,
)


class ProviderConfigError(RuntimeError):
    pass


_PROVIDERS: dict[str, type[Provider]] = {
    "echo": EchoProvider,
    "ollama": OllamaProvider,
    "openai_compatible": OpenAICompatibleProvider,
    "openai": OpenAICompatibleProvider,
    "litellm": LiteLLMProvider,
    "github_copilot": GitHubCopilotProvider,
}


def register_provider(name: str, provider_cls: type[Provider]) -> None:
    _PROVIDERS[name] = provider_cls


def get_provider(name: str) -> Provider:
    provider_cls = _PROVIDERS.get(name)
    if provider_cls is None:
        available = ", ".join(sorted(_PROVIDERS))
        raise ProviderConfigError(f"Unknown provider '{name}'. Available providers: {available}")
    try:
        instance = provider_cls()
    except ProviderInvocationError as exc:
        # E.g. LiteLLMProvider raises when its optional dep is missing.
        raise ProviderConfigError(str(exc)) from exc
    return _wrap_with_policy(name, instance)


def _wrap_with_policy(name: str, instance: Provider) -> Provider:
    """Apply the per-provider retry + circuit-breaker policy if configured.

    Deferred import: :mod:`brains.providers.policy` imports the registry
    to subclass ``Provider``, so we resolve it lazily to avoid a circle.
    The wrap is a no-op (returns the raw instance) when no policy entry
    exists for ``name``, so providers without a configured policy keep
    the historical behaviour byte-for-byte.
    """
    from brains.config import settings  # local import: settings is mutable in tests
    from brains.providers.policy import ResilientProvider, resolve_policy

    policy = resolve_policy(name, settings)
    if not policy.retry_enabled and not policy.circuit_enabled:
        return instance
    return ResilientProvider(instance, provider_name=name, policy=policy)


def available_providers() -> list[str]:
    """Names of all registered providers (used by the admin UI)."""
    return sorted(_PROVIDERS)


def is_stub_provider(name: str) -> bool:
    """Return ``True`` when *name* refers to a built-in stub provider
    (e.g. ``echo``). Cheap class-attribute lookup — no instantiation,
    safe to call on the request hot path. Unknown names return
    ``False`` so callers default to "treat as real upstream"."""
    provider_cls = _PROVIDERS.get(name)
    if provider_cls is None:
        return False
    return bool(getattr(provider_cls, "is_stub", False))


def stub_provider_names() -> list[str]:
    """Sorted list of every registered stub-provider name. Used by the
    admin UI to badge stub rows."""
    return sorted(name for name, cls in _PROVIDERS.items() if getattr(cls, "is_stub", False))


__all__ = [
    "ProviderConfigError",
    "ProviderInvocationError",
    "available_providers",
    "get_provider",
    "is_stub_provider",
    "register_provider",
    "stub_provider_names",
]
