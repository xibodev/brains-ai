"""Require a release tag to match the package version exactly."""

from __future__ import annotations

import argparse
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"


def project_version(path: Path = PYPROJECT) -> str:
    with path.open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def validate_release_tag(tag: str, version: str) -> None:
    expected = f"v{version}"
    if tag != expected:
        raise ValueError(
            f"release tag {tag!r} does not match package version {version!r}; expected {expected!r}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify that a release tag matches pyproject.toml."
    )
    parser.add_argument("tag", help="Git tag, for example v1.1.0")
    args = parser.parse_args()

    version = project_version()
    try:
        validate_release_tag(args.tag, version)
    except ValueError as exc:
        parser.error(str(exc))

    print(f"release tag {args.tag} matches package version {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
