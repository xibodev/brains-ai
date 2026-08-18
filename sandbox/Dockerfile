# Brains-v2 isolated sandbox image.
#
# Purpose: build and exercise brains (serve-all, mcp, wire, ...) WITHOUT
# touching the host's live brains install (~/.brains, ports 8787/9876/9877)
# or the operator's real agent-tool configs (~/.copilot, ~/.claude*,
# ~/.codex). Everything brains writes lives under the throwaway container
# user's HOME, which is discarded with the container.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# git: `brains upgrade` + general tooling. curl: container-side health probes.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Non-root user whose clean HOME anchors every piece of brains state.
RUN useradd --create-home --uid 1000 --shell /bin/bash agent

# Build the package into the image so it runs even without the live mount.
# At runtime the host repo is mounted read-only over /opt/brains, so source
# edits on the host are picked up live by the editable install.
WORKDIR /opt/brains
COPY . /opt/brains
RUN pip install -e '.[dev]'

# Isolated state dir + an empty workspace + simulated (empty) agent-tool
# config homes so `brains-ai wire --global` later writes HERE, never the host.
RUN mkdir -p /home/agent/.brains /home/agent/work \
             /home/agent/.copilot /home/agent/.codex /home/agent/.claude \
 && chown -R agent:agent /home/agent /opt/brains

USER agent
ENV HOME=/home/agent \
    BRAINS_STATE_DIR=/home/agent/.brains \
    BRAINS_DB_URL=sqlite:////home/agent/.brains/brains.db
WORKDIR /home/agent/work

EXPOSE 8787 9876 9877

# Default: init the isolated DB + admin key, then supervise the full stack
# (gateway + dashboard + MCP). MCP bind host comes from the env set in compose.
CMD ["bash", "-lc", "exec brains-ai up --gateway-host 0.0.0.0 --dashboard-host 0.0.0.0"]
