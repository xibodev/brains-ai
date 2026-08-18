"""Tier + provider selection façade.

Public surface is :func:`select_model`. Two modes, both delegated to
:mod:`brains.router.resolver`:

- **Routing enabled** (``settings.router.enabled = True`` — the
  default): the explicit ``brains/auto`` alias is honoured (classifier
  picks the tier). Tier aliases (``brains/cheap`` etc.), direct
  provider catalog matches, and Anthropic CLI shorthands all work too,
  none of which involve the classifier. Unknown ids return 404.

- **Routing disabled** (``settings.router.enabled = False``):
  ``brains/auto`` is refused (returns 404). Everything else still
  works because it's an explicit client choice. The only difference
  is brains can't silently classify even if asked to.

In neither mode does the classifier silently rewrite an explicit
client request. The flag only gates the explicit ``brains/auto``
opt-in path.
"""

from brains.config import settings
from brains.router.resolver import ModelNotFoundError, resolve_model

__all__ = ["select_model", "ModelNotFoundError"]


def select_model(classification, requested_model: str | None = None):
    """Resolve the request to ``{provider, provider_name, model, tier}``.

    Raises :class:`ModelNotFoundError` when the requested model is
    neither a real provider-catalog entry, nor a tier alias, nor an
    Anthropic CLI shorthand — or is ``brains/auto`` with routing
    disabled. The API handlers catch this and emit 404.
    """
    return resolve_model(
        requested_model,
        classification,
        allow_classifier=settings.router.enabled,
    )
