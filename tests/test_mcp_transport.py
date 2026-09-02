from brains.mcp.transport import (
    MCP_CLIENT_BEARER_ENV,
    MCP_LEGACY_SSE_PATH,
    MCP_MODE_SSE,
    MCP_MODE_STDIO,
    MCP_MODE_STREAMABLE_HTTP,
    MCP_STREAMABLE_HTTP_PATH,
    mcp_http_url,
)


def test_mcp_transport_contract() -> None:
    assert MCP_MODE_STREAMABLE_HTTP == "streamable-http"
    assert MCP_MODE_STDIO == "stdio"
    assert MCP_MODE_SSE == "sse"
    assert MCP_STREAMABLE_HTTP_PATH == "/mcp"
    assert MCP_LEGACY_SSE_PATH == "/sse"
    assert MCP_CLIENT_BEARER_ENV == "BRAINS_MCP_BEARER_TOKEN"


def test_mcp_http_url_uses_canonical_streamable_http_path() -> None:
    assert mcp_http_url() == "http://127.0.0.1:9877/mcp"
    assert mcp_http_url("localhost", 1234) == "http://localhost:1234/mcp"
