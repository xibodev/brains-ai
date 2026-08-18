"""Admin UI and API.

Provides operator-facing endpoints to inspect provider configuration,
test connectivity, and persist edits through a runtime overlay YAML.

Secrets are NEVER stored in the overlay. The admin UI takes an
environment-variable name reference of the form ``${ENV:NAME}``; the
config loader resolves it at read time. This keeps every key out of
the persisted YAML and lets the operator manage rotation through
their existing secret manager.
"""

from brains.admin.routes import router

__all__ = ["router"]
