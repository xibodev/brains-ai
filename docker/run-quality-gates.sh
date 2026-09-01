#!/usr/bin/env bash
set -euo pipefail

python scripts/check_docs.py
python scripts/check_traceability.py
ruff check .
ruff format --check .
mypy
pytest -q -m acceptance
pytest -q --maxfail=20
npm --prefix frontend run typecheck
npm --prefix tests/e2e run typecheck
python scripts/check_spa_bundle.py --no-install
uv build --no-build-isolation
python scripts/check_distribution.py
