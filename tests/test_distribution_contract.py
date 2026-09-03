from __future__ import annotations

import io
import tarfile
import zipfile

from scripts import check_distribution
from scripts.check_distribution import check_sdist, check_wheel


def test_wheel_contract_rejects_deleted_legacy_browser_files(tmp_path) -> None:
    wheel = tmp_path / "brains_ai-0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("brains/web/spa/index.html", "app")
        archive.writestr("brains/web/spa/assets/app.js", "app")
        archive.writestr("brains/web/templates/admin/login.html", "login")
        archive.writestr("brains/storage/baseline/sqlite.sql", "sqlite")
        archive.writestr("brains/storage/baseline/postgresql.sql", "postgres")
        archive.writestr("brains/storage/sql_migrations/001.sql", "migration")
        archive.writestr("brains/dashboard/app.py", "deleted")
    assert any("brains/dashboard/" in error for error in check_wheel(wheel))


def test_sdist_contract_rejects_deleted_legacy_browser_files(tmp_path) -> None:
    sdist = tmp_path / "brains_ai-0.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        for name in (
            "pyproject.toml",
            "src/brains/web/spa/index.html",
            "src/brains/storage/baseline/sqlite.sql",
            "src/brains/web/templates/dashboard/base.html",
        ):
            payload = b"test"
            info = tarfile.TarInfo(f"brains_ai-0/{name}")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    assert any("templates/dashboard/" in error for error in check_sdist(sdist))


def test_wheel_contract_rejects_rogue_member(tmp_path, monkeypatch) -> None:
    wheel = tmp_path / "brains_ai-0-py3-none-any.whl"
    members = [
        "brains/web/spa/index.html",
        "brains/web/spa/assets/app.js",
        "brains/web/templates/admin/login.html",
        "brains/storage/baseline/sqlite.sql",
        "brains/storage/baseline/postgresql.sql",
        "brains/storage/sql_migrations/001.sql",
    ]
    with zipfile.ZipFile(wheel, "w") as archive:
        for member in [*members, "brains/rogue.py"]:
            archive.writestr(member, "test")
    monkeypatch.setattr(
        check_distribution,
        "_manifest_inventory",
        lambda kind: sorted(members) if kind == "wheel" else [],
    )

    assert any("unexpected=1" in error for error in check_wheel(wheel))


def test_sdist_contract_rejects_missing_reviewed_member(tmp_path, monkeypatch) -> None:
    sdist = tmp_path / "brains_ai-0.tar.gz"
    members = [
        "pyproject.toml",
        "src/brains/web/spa/index.html",
        "src/brains/storage/baseline/sqlite.sql",
    ]
    with tarfile.open(sdist, "w:gz") as archive:
        for name in members:
            payload = b"test"
            info = tarfile.TarInfo(f"brains_ai-0/{name}")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    monkeypatch.setattr(
        check_distribution,
        "_manifest_inventory",
        lambda kind: sorted([*members, "src/brains/required.py"]) if kind == "sdist" else [],
    )

    assert any("missing=1" in error for error in check_sdist(sdist))


def test_wheel_contract_rejects_duplicate_member(tmp_path, monkeypatch) -> None:
    wheel = tmp_path / "brains_ai-0-py3-none-any.whl"
    members = [
        "brains/web/spa/index.html",
        "brains/web/spa/assets/app.js",
        "brains/web/templates/admin/login.html",
        "brains/storage/baseline/sqlite.sql",
        "brains/storage/baseline/postgresql.sql",
        "brains/storage/sql_migrations/001.sql",
    ]
    with zipfile.ZipFile(wheel, "w") as archive:
        for member in members:
            archive.writestr(member, "test")
        archive.writestr(members[0], "duplicate")
    monkeypatch.setattr(
        check_distribution,
        "_manifest_inventory",
        lambda kind: sorted(members) if kind == "wheel" else [],
    )

    assert any("unexpected=1" in error for error in check_wheel(wheel))


def test_sdist_contract_rejects_multiple_roots(tmp_path, monkeypatch) -> None:
    sdist = tmp_path / "brains_ai-0.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        for name in (
            "brains_ai-0/pyproject.toml",
            "other-root/src/brains/rogue.py",
        ):
            payload = b"test"
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    monkeypatch.setattr(check_distribution, "_manifest_inventory", lambda kind: [])

    assert any("sdist inventory is malformed" in error for error in check_sdist(sdist))


def test_sdist_contract_rejects_non_regular_member(tmp_path, monkeypatch) -> None:
    sdist = tmp_path / "brains_ai-0.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        payload = b"test"
        regular = tarfile.TarInfo("brains_ai-0/pyproject.toml")
        regular.size = len(payload)
        archive.addfile(regular, io.BytesIO(payload))
        link = tarfile.TarInfo("brains_ai-0/rogue-link")
        link.type = tarfile.SYMTYPE
        link.linkname = "pyproject.toml"
        archive.addfile(link)
    monkeypatch.setattr(check_distribution, "_manifest_inventory", lambda kind: [])

    assert any("sdist inventory is malformed" in error for error in check_sdist(sdist))
