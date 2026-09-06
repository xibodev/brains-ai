"""Shared MCP transport names and endpoint construction.

This module deliberately has no MCP SDK dependency so server, wiring, service,
and test code can agree on the public transport contract without importing the
runtime server.
"""

MCP_MODE_STREAMABLE_HTTP = "streamable-http"
MCP_MODE_STDIO = "stdio"
MCP_MODE_SSE = "sse"

MCP_STREAMABLE_HTTP_PATH = "/mcp"
MCP_LEGACY_SSE_PATH = "/sse"
MCP_CLIENT_BEARER_ENV = "BRAINS_MCP_BEARER_TOKEN"


def mcp_http_url(host: str = "127.0.0.1", port: int = 9877) -> str:
    """Return the canonical Streamable HTTP endpoint for a Brains server."""

    return f"http://{host}:{port}{MCP_STREAMABLE_HTTP_PATH}"


__all__ = [
    "MCP_CLIENT_BEARER_ENV",
    "MCP_LEGACY_SSE_PATH",
    "MCP_MODE_SSE",
    "MCP_MODE_STDIO",
    "MCP_MODE_STREAMABLE_HTTP",
    "MCP_STREAMABLE_HTTP_PATH",
    "mcp_http_url",
]
