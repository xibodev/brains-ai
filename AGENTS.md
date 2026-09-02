<!--
last_verified: 2026-08-04T08:00:00.000-06:00
verified_by: GitHub Copilot CLI
verification_basis: HEAD c21a15db3859e6b9f147260a38a7a0d6fe2533b2 plus the local blocking-quality-gates change; static inspection of repository instructions and canonical product and quality contracts; deployment not verified
-->

# Agent Instructions

## STOP — this repository is public

A commit is not a draft. Once pushed, treat it as permanently disclosed: clones,
forks and caches copy it within minutes, and a later force-push does not recall
it. Rotate first, purge second — removing a secret from history does not
un-disclose it.

**Check all four before every commit. File contents are only the first.**

1. **File contents** — no keys, tokens, passwords, private keys, connection
   strings, real hostnames, IPs, cloud account ids, or customer data.
2. **Commit messages** — a clean diff does not mean a clean commit. Messages are
   permanent and no working-tree scan covers them. Never paste logs, stack
   traces, curl commands or environment dumps into one.
3. **Author and committer identity** — `git log` carries the name and email of
   every commit, and those fields are not file content, so no content scan will
   ever surface them. A fresh clone inherits your *global* git identity. Verify:

   ```bash
   git log -1 --format='%an <%ae> / %cn <%ce>'
   ```

4. **Detection rules themselves.** A guard that spells out the identifiers it
   searches for publishes that list to everyone who reads the file — more useful
   to a stranger than to us. The privacy gate in `.github/workflows/ci.yml`
   therefore reads its patterns from the `PRIVACY_PATTERNS` repository secret.
   **Never inline them.** The same applies to any allowlist, denylist or test
   fixture built from real identifiers.

## Never commit configuration

Only `*.example` files, containing placeholders only — never a real value, not
even an expired or "test" one. Real hosts, digests and secrets belong in the
deployment repository or a secret store.

When a task needs a real credential, reference it **by path** and let the
program read it at runtime. Do not open it, paste it into a conversation, or
copy it into a fixture. Tests must synthesise their own key material.

Never log a secret and never put one in an error message — errors get pasted
into issues.

## Releases and PyPI

`brains-ai` is published from this repository through **PyPI Trusted Publishers
(OIDC)**, which is bound to one owner/repository pair. If the repository moves,
a matching publisher must be registered before the first tag push or PyPI will
reject it. See the setup notes in `.github/workflows/release.yml`.

PyPI versions are immutable and resolution picks the **highest** version, so a
version number can never be reused or reset — a lower version would publish but
never install. The tag and `pyproject.toml` must agree; CI enforces this.


- The product is **Brains**. Keep the repository, `brains-ai` package and CLI, `brains` namespace, `brains_` MCP prefix, `~/.brains` state, and browser product aligned to that identity.
- Start with [PRODUCT_BRIEF.md](docs/product/PRODUCT_BRIEF.md), then map work through [FEATURE_CONTRACT.md](docs/product/FEATURE_CONTRACT.md), [PERSONAS_AND_JOURNEYS.md](docs/product/PERSONAS_AND_JOURNEYS.md), and [TRACEABILITY.md](docs/product/TRACEABILITY.md).
- [BACKLOG.md](docs/product/BACKLOG.md) is the sole schedulable core backlog. [FROZEN_BACKLOG.md](docs/product/FROZEN_BACKLOG.md) contains deferred TODOs that cannot be scheduled until the core backlog is empty and a human explicitly approves thawing them. Keep both files action-only; remove completed items and historical/evidence narrative and rely on Git history.
- Do implementation work on local feature or fix branches and merge accepted work into local `staging`. Do not commit directly to `main`, push any branch, merge into `main`, tag, publish, or release without the operator's explicit approval.
- Any test that could affect the installed Brains service, database, state directory, ports, or client configuration must run in Docker with isolated synthetic state and configuration. Docker tests may use outbound internet when needed, but must never mount the operator's `~/.brains` or client configuration, bind host ports 9876 or 9877, or send requests or mutations to the host installation.
- Plan, implement, review, test, and UAT against the final product outcome and stable `F*`, `B*`, `J*`, and `AC-*` IDs.
- Keep implementation minimal but functional.
- SQLite is the default source of truth; Markdown projections under `.brains/views` are optional.
- Do not bypass `require_api_key` or `require_console_auth` on protected `/v1/*` routes.
- Pattern approval, recurring execution, tool spawning, and outward effects remain human-governed. Do not strengthen documentation claims beyond current enforcement.
- Distinguish current facts, target contracts, and evidence gaps.
- Do not add release chronology, milestone diaries, dated test counts, logbooks, saga reports, screenshot proof packs, or tag-based truth.
- Update canonical docs and traceability when routes, components, APIs, models, migrations, CLI/MCP families, tests, or operational contracts change.
- Run `python scripts/check_docs.py`, `python scripts/check_traceability.py`, and the smallest relevant tests before handoff. `python scripts/run_quality_gates.py` runs the full local gate in CI order.
