"""Persistent admin/API-key management.

Brains needs a single shared secret to gate the gateway API (`/v1/*`) and
the modern operator console (`/app`, with `/admin/login` and `/admin/logout`). Historically the
default was the literal string ``"local-dev-key"`` which made the install
flow trivial but also meant the project shipped with default credentials —
not acceptable for the public alpha.

This module replaces the literal default with:

1. **Operator-set override** — if ``BRAINS_API_KEY`` (or ``settings.api_key``
   loaded from ``brains.runtime.yaml`` / ``BRAINS_CONFIG``) is non-empty,
   that value wins. Nothing is generated, nothing is written to disk.
2. **Persisted local key** — otherwise, the runtime reads
   ``~/.brains/admin-key`` (or ``$BRAINS_STATE_DIR/admin-key``). The file
   is created with mode ``0600`` on POSIX so other users on the host can't
   read it; on Windows it inherits the parent ACL.
3. **First-run auto-generation** — if no file exists, a 32-byte URL-safe
   random token is generated, written to that path, and the operator is
   shown a one-time banner on stderr with the key value, the file path,
   and the local login URL.

The key is loaded into ``settings.api_key`` in-process so the rest of the
auth machinery (``brains.api.auth``) keeps working unchanged.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import sys
from pathlib import Path


def state_dir() -> Path:
    """Resolve the brains state directory.

    Honors ``BRAINS_STATE_DIR`` (used by the OS installers under
    ``install/``); otherwise defaults to ``~/.brains``.
    """
    override = os.environ.get("BRAINS_STATE_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".brains"


def admin_key_path() -> Path:
    """Return the absolute path to the persisted admin key file."""
    return state_dir() / "admin-key"


def _generate_key() -> str:
    return secrets.token_urlsafe(32)


def _write_key_file(path: Path, key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key + "\n", encoding="utf-8")
    if os.name != "nt":
        # Best-effort; some filesystems (NFS, FAT) don't support chmod.
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)


def read_persisted_key() -> str | None:
    """Read the persisted key from disk, or ``None`` if absent/empty."""
    path = admin_key_path()
    if not path.exists():
        return None
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def ensure_admin_key(
    *,
    print_banner: bool = False,
    host: str = "127.0.0.1",
    port: int = 8787,
) -> tuple[str, bool]:
    """Make sure ``settings.api_key`` is populated; return ``(key, was_generated)``.

    Resolution order:
        1. ``settings.api_key`` is already set → use it. ``was_generated`` is
           ``False``.
        2. ``~/.brains/admin-key`` exists → load it into ``settings.api_key``.
           ``was_generated`` is ``False``.
        3. Otherwise → generate a random key, persist it, and (if
           ``print_banner=True``) print the one-time banner to stderr.
           ``was_generated`` is ``True``.

    ``settings.api_key`` is mutated in-process so the running app sees the
    new value without a restart.
    """
    from brains.config import settings

    def apply_secure(key: str) -> None:
        # This runs after config module initialization. During very early
        # bootstrap (before the DB can open), leave defaults intact so repair
        # commands remain available.
        try:
            from brains.control.secure_settings import apply_to_settings

            apply_to_settings(settings, key)
        except Exception:
            pass

    if settings.api_key:
        apply_secure(settings.api_key)
        return settings.api_key, False

    persisted = read_persisted_key()
    if persisted:
        settings.api_key = persisted
        apply_secure(persisted)
        return persisted, False

    key = _generate_key()
    path = admin_key_path()
    _write_key_file(path, key)
    settings.api_key = key
    apply_secure(key)
    if print_banner:
        print_first_run_banner(key=key, path=path, host=host, port=port)
    return key, True


def rotate_admin_key() -> str:
    """Generate a new key, overwrite the key file, update ``settings.api_key``.

    Any active browser cookies are invalidated automatically because the
    cookie signature is keyed by the API key value itself (see
    ``brains.api.auth.mint_browser_token``).

    Rotation is the *explicit* supersede path for the admin key: it names the
    key it just overwrote and revokes exactly that hash, then registers the new
    one. Nothing is inferred from what is or is not left on disk, so a rotation
    performed here denies the old key install-wide on the very next request,
    while a process that merely cannot see the key file revokes nothing.
    """
    from brains.config import settings

    if os.environ.get("BRAINS_API_KEY"):
        raise RuntimeError(
            "BRAINS_API_KEY is controlled by the process environment; rotate it in "
            "that authoritative store, restart Brains, then use admin-key rotation "
            "only for file-managed installs"
        )

    superseded = settings.api_key or read_persisted_key()
    key = _generate_key()
    if superseded:
        # Ciphertext is keyed by the admin secret. Re-key before replacing the
        # file so any failure leaves both the old key and old ciphertext usable.
        from brains.control.secure_settings import rekey_all

        rekey_all(superseded, key)
    _write_key_file(admin_key_path(), key)
    settings.api_key = key
    _supersede_admin_key(superseded, key)
    return key


def _supersede_admin_key(superseded: str | None, key: str) -> None:
    """Revoke the exact key that was rotated out, then adopt the new one.

    Best-effort against the store: a store that cannot be reached must not stop
    the key file from being rotated - the operator would otherwise be left with
    a key on disk that the process refuses to reload.

    A superseded value that is *also* listed in ``settings.api_keys`` is left
    alone: the operator still declares it as an accepted key, and rotating the
    bootstrap key was not a request to retire it.
    """
    try:
        from brains.authz import credentials as creds
        from brains.config import settings
        from brains.control.operators import ensure_admin_operator

        creds.invalidate_source_cache()
        if superseded and superseded != key and superseded not in set(settings.api_keys or ()):
            creds.revoke_local_secret(superseded)
        try:
            operator_id = ensure_admin_operator()["id"]
        except Exception:  # pragma: no cover - defensive
            operator_id = None
        creds.register_credential(
            key,
            kind=creds.KIND_ADMIN,
            operator_id=operator_id,
            label="adopted local key",
            source=creds.SOURCE_ADMIN_KEY,
            reinstate=True,
        )
        creds.sync_local_credentials()
    except Exception:  # pragma: no cover - defensive
        pass


def print_first_run_banner(
    *, key: str, path: Path, host: str = "127.0.0.1", port: int = 8787
) -> None:
    """Print a one-time setup banner to stderr when a key is auto-generated."""
    bar = "=" * 72
    msg = (
        f"\n{bar}\n"
        "  Brains generated a new admin/API key (first run)\n"
        f"{bar}\n\n"
        f"  Key      : {key}\n"
        f"  Stored at: {path}\n"
        f"  Sign in  : http://{host}:{port}/admin/login\n\n"
        "  Use this key as the password on the admin login page, or send it\n"
        "  via 'Authorization: Bearer <key>' for the JSON API.\n\n"
        "  - Treat it as a secret. Anyone with this key controls the gateway.\n"
        "  - Override with the BRAINS_API_KEY env var.\n"
        "  - Rotate any time with: brains-ai admin-key rotate\n"
        "  - View later with     : brains-ai admin-key show\n\n"
        f"{bar}\n"
    )
    sys.stderr.write(msg)
    sys.stderr.flush()


__all__ = [
    "admin_key_path",
    "ensure_admin_key",
    "print_first_run_banner",
    "read_persisted_key",
    "rotate_admin_key",
    "state_dir",
]
