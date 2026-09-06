from __future__ import annotations

import io
import stat
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

from scripts import check_distribution
from scripts.check_distribution import check_sdist, check_wheel

SUPPORTED_PYTHON_MINORS = ["3.11", "3.12"]
LEGACY_INSTALLER_PATHS = (
    "install/install-windows.ps1",
    "install/uninstall-windows.ps1",
    "install/install-launchd.sh",
    "install/uninstall-launchd.sh",
    "install/install-systemd.sh",
    "install/uninstall-systemd.sh",
)


def test_supported_python_bounds_match_classifiers_and_canonical_docs() -> None:
    root = check_distribution.ROOT
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert project["requires-python"] == ">=3.11,<3.13"
    assert [
        classifier.rsplit(" :: ", 1)[-1]
        for classifier in project["classifiers"]
        if classifier.startswith("Programming Language :: Python :: 3.")
    ] == SUPPORTED_PYTHON_MINORS
    assert "Brains requires Python 3.11 or 3.12." in (root / "README.md").read_text(
        encoding="utf-8"
    )
    assert "Brains supports Python 3.11 and 3.12." in (root / "docs" / "OPERATIONS.md").read_text(
        encoding="utf-8"
    )


def test_only_canonical_installer_identity_and_executable_remain() -> None:
    root = check_distribution.ROOT
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert project["scripts"] == {"brains-ai": "brains.cli.app:app"}
    assert all(not (root / Path(path)).exists() for path in LEGACY_INSTALLER_PATHS)

    from brains.service.common import LAUNCHD_LABEL, SYSTEMD_UNIT, WINDOWS_TASK_NAME

    assert WINDOWS_TASK_NAME == "BrainsServeAll"
    assert LAUNCHD_LABEL == "com.brains.serve-all"
    assert SYSTEMD_UNIT == "brains-serve-all.service"


def test_source_inventory_is_normalized_and_tracks_product_source(tmp_path) -> None:
    for name in check_distribution.SOURCE_TOP_LEVEL:
        (tmp_path / name).write_text(name, encoding="utf-8")
    source = tmp_path / "src" / "brains"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("", encoding="utf-8")
    (source / "nested").mkdir()
    (source / "nested" / "feature.py").write_text("", encoding="utf-8")
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "ignored.py").write_text("", encoding="utf-8")
    (source / "ignored.pyc").write_bytes(b"ignored")

    before = check_distribution.source_inventory(tmp_path)
    assert before == sorted(
        [
            *check_distribution.SOURCE_TOP_LEVEL,
            "src/brains/__init__.py",
            "src/brains/nested/feature.py",
        ]
    )

    (source / "added.py").write_text("", encoding="utf-8")
    (source / "nested" / "feature.py").unlink()
    after = check_distribution.source_inventory(tmp_path)
    assert "src/brains/added.py" in after
    assert "src/brains/nested/feature.py" not in after


def test_source_inventory_rejects_missing_package_input(tmp_path) -> None:
    (tmp_path / "src" / "brains").mkdir(parents=True)

    with pytest.raises(ValueError, match="required product source input is unavailable"):
        check_distribution.source_inventory(tmp_path)


@pytest.mark.parametrize("missing", ["wheel", "sdist"])
def test_distribution_inventory_requires_both_fresh_artifact_kinds(tmp_path, missing) -> None:
    root = tmp_path / "root"
    (root / "src" / "brains").mkdir(parents=True)
    dist = tmp_path / "dist"
    dist.mkdir()
    if missing != "wheel":
        with zipfile.ZipFile(dist / "brains_ai-0-py3-none-any.whl", "w") as archive:
            archive.writestr("brains/__init__.py", "")
    if missing != "sdist":
        with tarfile.open(dist / "brains_ai-0.tar.gz", "w:gz") as archive:
            payload = b""
            info = tarfile.TarInfo("brains_ai-0/pyproject.toml")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))

    with pytest.raises(ValueError, match="fresh wheel and sdist are required"):
        check_distribution.distribution_inventory(dist, root)


def test_distribution_inventory_rejects_semantically_incomplete_artifacts(tmp_path) -> None:
    root = tmp_path / "root"
    (root / "src" / "brains").mkdir(parents=True)
    for name in check_distribution.SOURCE_TOP_LEVEL:
        (root / name).write_text(name, encoding="utf-8")
    dist = tmp_path / "dist"
    dist.mkdir()
    with zipfile.ZipFile(dist / "brains_ai-0-py3-none-any.whl", "w") as archive:
        archive.writestr("brains/__init__.py", "")
    with tarfile.open(dist / "brains_ai-0.tar.gz", "w:gz") as archive:
        payload = b""
        info = tarfile.TarInfo("brains_ai-0/pyproject.toml")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))

    with pytest.raises(ValueError, match="violates the artifact contract"):
        check_distribution.distribution_inventory(dist, root)


def test_distribution_inventory_rejects_multiple_artifacts(tmp_path) -> None:
    root = tmp_path / "root"
    (root / "src" / "brains").mkdir(parents=True)
    dist = tmp_path / "dist"
    dist.mkdir()
    for version in ("0", "1"):
        with zipfile.ZipFile(dist / f"brains_ai-{version}-py3-none-any.whl", "w") as archive:
            archive.writestr("brains/__init__.py", "")
    with tarfile.open(dist / "brains_ai-0.tar.gz", "w:gz") as archive:
        payload = b""
        info = tarfile.TarInfo("brains_ai-0/pyproject.toml")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))

    with pytest.raises(ValueError, match="fresh wheel and sdist are required"):
        check_distribution.distribution_inventory(dist, root)


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


def test_wheel_contract_rejects_non_regular_member(tmp_path, monkeypatch) -> None:
    wheel = tmp_path / "brains_ai-0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        link = zipfile.ZipInfo("brains/rogue-link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "target")
    monkeypatch.setattr(check_distribution, "_manifest_inventory", lambda kind: [])

    assert any("wheel inventory is malformed" in error for error in check_wheel(wheel))
