from __future__ import annotations

import io
import tarfile
import zipfile

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
