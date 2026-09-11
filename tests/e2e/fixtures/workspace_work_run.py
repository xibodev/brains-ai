"""Run the copied-source Work browser suite in a disposable, unmounted container.

Build with workspace_work.Dockerfile from the repo root. Run on an internal Docker
network without host ports or mounts. Export /tmp/workspace-work-artifacts with
docker cp, then delete the owned container, image, network and builder.
Pass --rebuild-bundle when intentionally refreshing generated assets: a second
build must match before browser testing. Default mode checks the stored bundle.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen


def main() -> int:
    if not Path("/.dockerenv").exists():
        raise RuntimeError("This runner must never start Brains or npm on the host")
    root = Path(__file__).resolve().parents[3]
    artifacts = Path("/tmp/workspace-work-artifacts")
    artifacts.mkdir(exist_ok=True)
    home = Path("/tmp/workspace-work-home")
    state = home / "state"
    state.mkdir(parents=True, exist_ok=True)
    if (state / "brains.db").exists():
        raise RuntimeError("Start a fresh container; this runner requires a fresh database")
    config = state / "brains.yaml"
    config.write_text("{}\n")
    os.environ.update(
        {
            "HOME": str(home),
            "BRAINS_STATE_DIR": str(state),
            "BRAINS_DB_URL": f"sqlite:///{state}/brains.db",
            "BRAINS_CONFIG": str(config),
            "BRAINS_RUNTIME_OVERLAY": str(state / "brains.runtime.yaml"),
            "BRAINS_API_KEY": "synthetic-container-admin-key",
            "BRAINS_E2E_WORK_DRIVER": "1",
            "BRAINS_PREWARM_INDEX_ON_SESSION": "0",
            "BRAINS_ALLOW_UNAUTHENTICATED_API": "0",
            "BRAINS_E2E_PYTHON": sys.executable,
        }
    )

    def run(args: list[str], name: str, cwd: Path = root) -> int:
        with (artifacts / f"{name}.log").open("w") as output:
            result = subprocess.run(args, cwd=cwd, stdout=output, stderr=subprocess.STDOUT)
        print(f"{name}: exit {result.returncode}", flush=True)
        return result.returncode

    def hashes(folder: Path) -> dict:
        return {
            str(path.relative_to(folder)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(folder.rglob("*"))
            if path.is_file()
        }

    source = hashes(root / "frontend/src")
    bundle = root / "src/brains/web/spa"
    supplied = hashes(bundle)
    if run(["npm", "run", "build"], "bundle-rebuild", root / "frontend"):
        return 1
    rebuilt = hashes(bundle)
    if "--rebuild-bundle" in sys.argv:
        if run(["npm", "run", "build"], "bundle-parity-rebuild", root / "frontend"):
            return 1
        if hashes(bundle) != rebuilt:
            raise RuntimeError("Repeated SPA build differs; do not export generated assets")
    identity = {
        "source": source,
        "supplied": supplied,
        "rebuilt": rebuilt,
        "matches": supplied == rebuilt,
        "rebuild_requested": "--rebuild-bundle" in sys.argv,
        "repeat_build_matches": hashes(bundle) == rebuilt,
    }
    (artifacts / "bundle-identity.json").write_text(json.dumps(identity, indent=2))
    if supplied != rebuilt and "--rebuild-bundle" not in sys.argv:
        print("Stored SPA differs from rebuilt source; see bundle-identity.json", flush=True)
        return 1
    codes = [
        run([sys.executable, "scripts/check_docs.py"], "docs"),
        run([sys.executable, "scripts/check_traceability.py"], "traceability"),
        run(["npm", "run", "typecheck"], "frontend-typecheck", root / "frontend"),
        run(["npm", "run", "typecheck"], "e2e-typecheck", root / "tests/e2e"),
    ]
    seeded = subprocess.check_output(
        [sys.executable, "tests/e2e/fixtures/workspace_work.py", "seed"],
        cwd=root,
        text=True,
    )
    manifest = json.loads(seeded)
    os.environ.update(
        {
            "BRAINS_E2E_KEY": manifest["key"],
            "BRAINS_E2E_ORG": manifest["org"],
            "BRAINS_E2E_WORK_MANIFEST": json.dumps(manifest),
        }
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    os.environ["BRAINS_E2E_BASE_URL"] = url
    os.environ["PLAYWRIGHT_HTML_OUTPUT_DIR"] = str(artifacts / "report")
    with (artifacts / "server.log").open("w") as log:
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "brains.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=root,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            for _ in range(120):
                if server.poll() is not None:
                    raise RuntimeError("Main app exited; inspect server.log")
                try:
                    with urlopen(url + "/admin/login", timeout=1) as response:
                        if response.status == 200:
                            break
                except OSError:
                    time.sleep(0.5)
            else:
                raise RuntimeError("Main app did not become ready")
            codes.append(
                run(
                    [
                        "npx",
                        "--no-install",
                        "playwright",
                        "test",
                        "workspace-work.spec.ts",
                        "--output",
                        str(artifacts / "results"),
                        "--workers=1",
                        "--retries=0",
                    ],
                    "playwright",
                    root / "tests/e2e",
                )
            )
        finally:
            server.terminate()
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
    return int(any(codes))


if __name__ == "__main__":
    raise SystemExit(main())
