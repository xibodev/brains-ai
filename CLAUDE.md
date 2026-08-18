# Working in this repository

See [AGENTS.md](AGENTS.md). It applies to every agent and every contributor.

The short version:

- **This repository is public.** Check file contents, commit messages, author identity, AND the detection rules themselves before every commit.
- **Never inline privacy patterns** — the CI gate reads them from the `PRIVACY_PATTERNS` secret, because a guard that names what it hunts publishes that list.
- **Never commit configuration** — only `*.example`, placeholders only.
- **Reference real credentials by path**; never read, paste, or fixture them.
- **PyPI versions are immutable and cannot be reset** — they only ever go up.
