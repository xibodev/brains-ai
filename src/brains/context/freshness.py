import hashlib
import ipaddress
import os
import socket
import subprocess
from urllib.parse import urlparse

import httpx

from brains.config import settings
from brains.control.events import append_event
from brains.storage.repositories import write_freshness_check

_MAX_REDIRECTS = 5
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


def _is_safe_external_url(url: str) -> tuple[bool, str | None]:
    """Validate that a URL targets a public network address.

    Returns ``(True, None)`` when the URL is safe to fetch, or
    ``(False, reason)`` when it targets a loopback / private / link-local /
    reserved address, has an unsupported scheme, fails DNS resolution, or
    is otherwise rejected.

    Operators can override the default-deny on internal targets by listing
    the hostname (exact match) in ``settings.source_allowlist`` — useful
    for legitimate internal documentation servers.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False, f"unsupported scheme: {parsed.scheme or '<empty>'}"
    host = parsed.hostname
    if not host:
        return False, "missing host"
    allowlist = settings.source_allowlist or ()
    if host in allowlist:
        # Explicit operator allow — bypass the IP-class check so internal
        # documentation hosts can be reached when intentionally permitted.
        return True, None
    if allowlist and host not in allowlist:
        # Backwards-compatible allowlist semantics: when the allowlist is
        # non-empty, any host not on it is rejected.
        return False, "source host is not allowlisted"
    # Default-deny on loopback / private / link-local / reserved targets to
    # prevent the gateway from being used as an SSRF probe against the host
    # network (cloud metadata, RFC1918 services, etc.).
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        return False, f"dns resolution failed: {exc}"
    for info in infos:
        ip_str = str(info[4][0])
        # Strip IPv6 zone identifier if present.
        ip_str = ip_str.split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False, f"target resolves to internal address: {ip_str}"
    return True, None


def _check_http_source(source: str) -> dict:
    """Walk redirects manually, validating each hop for SSRF safety."""
    current = source
    redirect_chain: list[str] = []
    for _ in range(_MAX_REDIRECTS + 1):
        ok, reason = _is_safe_external_url(current)
        if not ok:
            return {
                "type": "web_url",
                "source": source,
                "ok": False,
                "error": reason,
                "redirect_chain": list(redirect_chain) or None,
            }
        try:
            response = httpx.head(
                current,
                follow_redirects=False,
                timeout=settings.freshness_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            return {
                "type": "web_url",
                "source": source,
                "ok": False,
                "error": str(exc),
                "redirect_chain": list(redirect_chain) or None,
            }
        location = response.headers.get("location")
        if response.status_code in _REDIRECT_STATUSES and location:
            redirect_chain.append(current)
            next_url = httpx.URL(location)
            if not next_url.is_absolute_url:
                next_url = httpx.URL(current).join(next_url)
            current = str(next_url)
            continue
        return {
            "type": "web_url",
            "ok": True,
            "status": response.status_code,
            "etag": response.headers.get("etag"),
            "last_modified": response.headers.get("last-modified"),
            "redirect_chain": list(redirect_chain) or None,
        }
    return {
        "type": "web_url",
        "source": source,
        "ok": False,
        "error": "too many redirects",
        "redirect_chain": list(redirect_chain),
    }


def check_source(source: str):
    if source.startswith("http"):
        result = _check_http_source(source)
        write_freshness_check(source, "web_url", result)
        if not result.get("ok"):
            message = (
                "web freshness rejected by allowlist"
                if result.get("error") == "source host is not allowlisted"
                else "web freshness failed"
            )
        else:
            message = "web freshness checked"
        append_event("freshness_check", message, metadata=result)
        return result
    if os.path.isfile(source):
        digest = hashlib.sha256()
        with open(source, "rb") as file_handle:
            for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
                digest.update(chunk)
        result = {
            "type": "local_file",
            "ok": True,
            "hash": digest.hexdigest(),
            "mtime": os.path.getmtime(source),
        }
        write_freshness_check(source, "local_file", result)
        append_event("freshness_check", "local file freshness checked", metadata=result)
        return result
    result = {"type": "git_repo", "source": source, "ok": True}
    if os.path.isdir(source):
        try:
            commit = subprocess.check_output(
                ["git", "-C", source, "rev-parse", "HEAD"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=5,
            ).strip()
            branch = subprocess.check_output(
                ["git", "-C", source, "rev-parse", "--abbrev-ref", "HEAD"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=5,
            ).strip()
            remote = subprocess.check_output(
                ["git", "-C", source, "remote", "get-url", "origin"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=5,
            ).strip()
            result.update({"commit": commit, "branch": branch, "remote": remote})
        except Exception:
            result["ok"] = False
            result["error"] = "git metadata unavailable"
    write_freshness_check(source, "git_repo", result)
    append_event("freshness_check", "git freshness checked", metadata=result)
    return result
