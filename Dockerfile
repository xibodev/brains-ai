# Build a slim runtime image for `brains-ai serve-all`.
#
# Image entrypoint runs the supervisor that brings up the gateway (:8787),
# browser/API service (:8787) and MCP server (:9877). State lives under /data which is
# meant to be a mounted volume so `brains.db`, `brains.runtime.yaml`, and
# `~/.brains` survive container restarts.
#
# Build:
#   docker build -t brains-ai .
# Run:
#   The default CMD (`serve-all`) binds the gateway to loopback
#   INSIDE the container (brains is loopback-first). To reach the published
#   ports from the host, tell serve-all to bind 0.0.0.0 (the container is the
#   isolation boundary; the API key auth still applies). MCP additionally
#   needs BRAINS_MCP_ALLOW_PUBLIC=1 to skip its loopback Host-header check.
#     docker run --rm -p 8787:8787 -p 9877:9877 \
#       -e BRAINS_MCP_BIND=0.0.0.0 -e BRAINS_MCP_ALLOW_PUBLIC=1 \
#       -v brains-data:/data brains-ai \
#       serve-all --gateway-host 0.0.0.0
#
# Multi-arch publish is handled by .github/workflows/release.yml using buildx.

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    BRAINS_STATE_DIR=/data/.brains \
    HOME=/data

RUN apt-get update \
 && apt-get install -y --no-install-recommends tini ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 1000 brains \
 && useradd  --system --uid 1000 --gid brains --home-dir /data --shell /sbin/nologin brains \
 && mkdir -p /data /data/.brains \
 && chown -R brains:brains /data

WORKDIR /app
COPY --chown=brains:brains pyproject.toml README.md LICENSE ./
COPY --chown=brains:brains src ./src

RUN pip install --no-cache-dir .

USER brains
WORKDIR /data
VOLUME ["/data"]

EXPOSE 8787 9877

# The supervisor can remain alive while a child crash-loops, so verify the
# gateway response plus a real authenticated MCP initialize + tools/list.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; \
assert urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=3).status == 200; \
from brains.service.common import mcp_protocol_status; \
assert mcp_protocol_status(timeout=3)['ready']" \
  || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "brains-ai"]
CMD ["serve-all"]
