# Build from the repository root; runtime requires --network <internal-network>,
# no published ports and no mounts. All source, dependencies and browsers are copied.
FROM mcr.microsoft.com/playwright:v1.61.1-noble
COPY --from=ghcr.io/astral-sh/uv:python3.12-bookworm-slim /usr/local/bin/uv /usr/local/bin/uv
WORKDIR /workspace
ENV UV_PYTHON_INSTALL_DIR=/opt/python
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv python install 3.12 && uv sync --frozen --extra dev --no-install-project
COPY frontend/package.json frontend/package-lock.json ./frontend/
RUN npm ci --prefix frontend
COPY tests/e2e/package.json tests/e2e/package-lock.json ./tests/e2e/
RUN npm ci --prefix tests/e2e
COPY . .
ENV PATH="/workspace/.venv/bin:${PATH}" PYTHONPATH=/workspace/src
CMD ["python", "tests/e2e/fixtures/workspace_work_run.py"]
