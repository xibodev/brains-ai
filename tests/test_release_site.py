from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/sync_release_site.py"
SPEC = importlib.util.spec_from_file_location("sync_release_site", SCRIPT)
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)
REPOSITORY = "xibodev/brains-ai"
PROJECT = '[project]\nversion = "1.5.0"\n'
CAPABILITIES = 'CORE_MCP_TOOLS = frozenset({"first", "second"})\n'


def marker(name: str, value: str = "old") -> str:
    return f"<!-- brains:{name}:start -->{value}<!-- brains:{name}:end -->"


def page() -> bytes:
    return (
        "<!doctype html>\r\n<title>Unchanged</title>\r\n"
        + marker("release-version")
        + marker("mcp-count")
        + "<main>"
        + marker("release-history")
        + "</main>"
        + marker("release-version")
        + marker("mcp-count")
        + "\r\n<footer>Unchanged</footer>\r\n"
    ).encode()


def release(tag: str = "v1.5.0", **overrides) -> dict:
    return {
        "tag_name": tag,
        "name": tag,
        "body": "## Highlights\n- **Shared** `work` & coordination\n- Safe state\n## Other\n- Omit this",
        "published_at": "2026-09-08T12:00:00Z",
        "html_url": f"https://github.com/{REPOSITORY}/releases/tag/{tag}",
        "draft": False,
        "prerelease": False,
        **overrides,
    }


def test_sync_only_marked_regions_and_is_byte_idempotent(tmp_path):
    site = tmp_path / "index.html"
    original = page()
    site.write_bytes(original)
    assert sync.sync_site(site, [release()], PROJECT, CAPABILITIES, REPOSITORY)
    expected = original.replace(
        marker("release-version").encode(), marker("release-version", "1.5.0").encode()
    )
    expected = expected.replace(marker("mcp-count").encode(), marker("mcp-count", "2").encode())
    expected = expected.replace(
        marker("release-history").encode(),
        marker("release-history", sync.render_history([release()])).encode(),
    )
    assert site.read_bytes() == expected
    modified = site.stat().st_mtime_ns
    assert not sync.sync_site(site, [release()], PROJECT, CAPABILITIES, REPOSITORY)
    assert site.stat().st_mtime_ns == modified
    assert site.read_bytes() == expected
    assert list(tmp_path.iterdir()) == [site]


def test_history_contract_and_escaping():
    html = sync.render_history([release(name='A "safer" <release>')])
    assert '<article class="release-entry" id="release-v1-5-0">' in html
    assert (
        '<div class="release-meta"><h3><a href="https://github.com/xibodev/brains-ai/releases/tag/v1.5.0">v1.5.0</a></h3>'
        in html
    )
    assert '<time datetime="2026-09-08">Sep 8, 2026</time>' in html
    assert '<span class="release-badge">Latest release</span>' in html
    assert '<div class="release-details"><h4>A &quot;safer&quot; &lt;release&gt;</h4>' in html
    assert "<li>Shared work &amp; coordination</li>" in html
    assert "Omit this" not in html
    assert (
        '<a class="release-link" href="https://github.com/xibodev/brains-ai/releases/tag/v1.5.0">Full release notes</a>'
        in html
    )
    assert "<h4>" not in sync.render_history([release()])
    assert "<h4>" not in sync.render_history([release(name="1.5.0")])


def test_stable_filter_semver_latest_and_chronological_history():
    records = [
        release("v1.9.0", published_at="2026-09-09T12:00:00Z"),
        release("v1.10.0", published_at="2026-09-08T12:00:00Z"),
        release("v1.8.0", published_at="2026-09-10T12:00:00Z"),
        release("v99.0.0", draft=True),
        release("v98.0.0", prerelease=True),
        release("v97.0.0rc1"),
        release("v96.0.0-beta.1"),
        release("v01.0.0"),
        release("$(touch nope)"),
    ]
    stable = sync.stable_releases(records, REPOSITORY)
    assert [item["tag_name"] for item in stable] == ["v1.10.0", "v1.8.0", "v1.9.0"]
    assert sync.stable_releases(list(reversed(records)), REPOSITORY) == stable


def test_history_has_at_most_six_releases():
    records = [release(f"v1.{index}.0") for index in range(10)]
    html = sync.render_history(sync.stable_releases(records, REPOSITORY))
    assert html.count('<article class="release-entry"') == 6
    assert html.count("Latest release") == 1


@pytest.mark.parametrize("body", [None, "", "## Highlights\n\n## Other\nno", "---\n```"])
def test_empty_highlights_use_canonical_link(body):
    html = sync.render_history([release(body=body)])
    assert "<ul>" not in html
    assert "Full release notes" in html


def test_plain_fallback_and_highlight_limits():
    assert sync.highlights("## Release\n\nFirst **summary**.\nSecond line.") == ["First summary."]
    assert sync.highlights("## Highlights\nPlain text\n* Next item\n## Fixes\nNo") == [
        "Plain text",
        "Next item",
    ]
    assert sync.highlights("## HIGHLIGHTS\r\n- First\r\n## Other\r\nNo") == ["First"]
    assert sync.highlights("## Highlights\n" + ("- " + "a" * 600 + "\n") * 10) == ["a" * 500] * 6


@pytest.mark.parametrize(
    "hidden",
    [
        "<!--\n## Highlights\n- hidden metadata\n-->",
        "```markdown\n## Highlights\n- hidden example\n```",
        "~~~markdown\n## Highlights\n- hidden example\n~~~~",
        "````\n```\n## Highlights\n- hidden example\n~~~\n```` trailing\n`````",
    ],
)
def test_hidden_headings_do_not_override_authored_highlights(hidden):
    body = hidden + "\n## What's Changed\n- Generated notes\n## Highlights\n- Authored benefit"
    assert sync.highlights(body) == ["Authored benefit"]
    assert sync.highlights(hidden + "\nActual fallback") == ["Actual fallback"]


@pytest.mark.parametrize(
    "body",
    [
        "<!--\n## Highlights\n- hidden metadata",
        "```\nHidden body",
        "~~~~\nHidden body\n~~~",
        "<!-- hidden -->\n```\nHidden\n```\n<!-- hidden -->",
    ],
)
def test_hidden_only_notes_have_no_highlights(body):
    assert sync.highlights(body) == []
    assert "<ul>" not in sync.render_history([release(body=body)])


@pytest.mark.parametrize(
    "body",
    [
        "```html\n<!--example\n```\n## Highlights\n- Actual benefit",
        "~~~html <!--example\nHidden\n~~~\n## Highlights\n- Actual benefit",
        "<!--\n```html\n-->## Highlights\n- Actual benefit",
        "<!-- hidden -->```html\n<!--example\n```\n## Highlights\n- Actual benefit",
        "<!-- hidden\n-->~~~html\n<!--example\n~~~\n## Highlights\n- Actual benefit",
        "<!-- first --><!-- second -->## Highlights\n- Actual benefit",
    ],
)
def test_comment_and_fence_delimiters_respect_active_block(body):
    assert sync.highlights(body) == ["Actual benefit"]


def test_inline_comment_removed_but_authored_html_is_escaped():
    body = '## Highlights\n- Actual <!-- hidden\nmetadata --> text <img src=x onerror="bad()">\n<!-- suppress rest\n- Hidden'
    assert sync.highlights(body) == ['Actual text <img src=x onerror="bad()">']
    html = sync.render_history([release(body=body)])
    assert "&lt;img" in html
    assert "<img" not in html
    assert "metadata" not in html
    assert "Hidden" not in html


def test_arbitrary_markdown_and_html_cannot_inject():
    html = sync.render_history(
        [
            release(
                name='<script>alert("title")</script>',
                body="## Highlights\n- <img src=x onerror=alert(1)>\n- [click](javascript:alert(1))\n- <!-- brains:release-version:start -->\n- <script>alert(1)</script>",
            )
        ]
    )
    assert "<script>" not in html
    assert "<img" not in html
    assert "javascript:" not in html
    assert "<!-- brains:" not in html
    assert "&lt;img" in html
    assert "&lt;script&gt;" in html


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "https://github.com/other/brains-ai/releases/tag/v1.5.0",
        "https://github.com/xibodev/brains-ai/releases/tag/v1.5.0?x=1",
        'https://github.com/xibodev/brains-ai/releases/tag/v1.5.0" onclick="alert(1)',
        "https://github.com.evil.test/xibodev/brains-ai/releases/tag/v1.5.0",
        "http://github.com/xibodev/brains-ai/releases/tag/v1.5.0",
    ],
)
def test_bad_release_urls_fail_closed(tmp_path, url):
    site = tmp_path / "index.html"
    site.write_bytes(page())
    with pytest.raises(ValueError, match="canonical"):
        sync.sync_site(site, [release(html_url=url)], PROJECT, CAPABILITIES, REPOSITORY)
    assert site.read_bytes() == page()


@pytest.mark.parametrize(
    "records",
    [
        [],
        {},
        [None],
        [release(prerelease=True)],
        [release(draft=True)],
        [release(), release()],
        [release(published_at=None)],
        [release(published_at="2026-02-30T12:00:00Z")],
        [release(published_at="yesterday")],
        [release(body={})],
        [release(draft="false")],
        [release(prerelease=None)],
    ],
)
def test_invalid_source_preserves_site(tmp_path, records):
    site = tmp_path / "index.html"
    site.write_bytes(page())
    with pytest.raises(ValueError):
        sync.sync_site(site, records, PROJECT, CAPABILITIES, REPOSITORY)
    assert site.read_bytes() == page()


@pytest.mark.parametrize("project", ['[project]\nversion="1.6.0"', "", "not toml"])
def test_tagged_version_must_match(tmp_path, project):
    site = tmp_path / "index.html"
    site.write_bytes(page())
    with pytest.raises(ValueError):
        sync.sync_site(site, [release()], project, CAPABILITIES, REPOSITORY)
    assert site.read_bytes() == page()


@pytest.mark.parametrize(
    "source",
    [
        "",
        'CORE_MCP_TOOLS = set(["x"])',
        "CORE_MCP_TOOLS = frozenset(get_tools())",
        "CORE_MCP_TOOLS = frozenset({model.call()})",
        'CORE_MCP_TOOLS = frozenset({"x"}, bad=True)',
        'CORE_MCP_TOOLS = {"x"}\nCORE_MCP_TOOLS = {"y"}',
        "CORE_MCP_TOOLS = {1}",
        'CORE_MCP_TOOLS = {""}',
        "CORE_MCP_TOOLS = frozenset()",
        'CORE_MCP_TOOLS = ["x"]',
    ],
)
def test_nonliteral_capabilities_fail_closed(tmp_path, source):
    site = tmp_path / "index.html"
    site.write_bytes(page())
    with pytest.raises(ValueError):
        sync.sync_site(site, [release()], PROJECT, source, REPOSITORY)
    assert site.read_bytes() == page()


def test_capabilities_are_never_executed(tmp_path):
    sentinel = tmp_path / "executed"
    source = f"from pathlib import Path\nPath({str(sentinel)!r}).touch()\n" + CAPABILITIES
    assert sync.tool_count(source) == 2
    assert not sentinel.exists()
    assert sync.tool_count('CORE_MCP_TOOLS: frozenset[str] = {"x", "x", "y"}') == 2


@pytest.mark.parametrize(
    "document",
    [
        "no markers",
        page().decode().replace(marker("release-history"), ""),
        page().decode() + marker("release-history"),
        page().decode() + marker("release-version"),
        page().decode() + marker("mcp-count"),
        page().decode().replace("<!-- brains:release-history:end -->", ""),
        page()
        .decode()
        .replace(
            marker("release-history"),
            "<!-- brains:release-history:end --><!-- brains:release-history:start -->",
        ),
        marker("release-history", marker("release-version")) + marker("mcp-count"),
    ],
)
def test_missing_duplicate_or_nested_markers_preserve_bytes(tmp_path, document):
    site = tmp_path / "index.html"
    site.write_bytes(document.encode())
    with pytest.raises(ValueError):
        sync.sync_site(site, [release()], PROJECT, CAPABILITIES, REPOSITORY)
    assert site.read_bytes() == document.encode()


def test_failed_atomic_replace_preserves_site(tmp_path, monkeypatch):
    site = tmp_path / "index.html"
    site.write_bytes(page())

    def fail(*args):
        raise OSError("replacement failed")

    monkeypatch.setattr(sync.os, "replace", fail)
    with pytest.raises(OSError, match="replacement failed"):
        sync.sync_site(site, [release()], PROJECT, CAPABILITIES, REPOSITORY)
    assert site.read_bytes() == page()
    assert list(tmp_path.iterdir()) == [site]


def test_cli_end_to_end_and_malformed_json(tmp_path):
    site = tmp_path / "index.html"
    site.write_bytes(page())
    releases = tmp_path / "releases.json"
    releases.write_text(json.dumps([release()]))
    project = tmp_path / "pyproject.toml"
    project.write_text(PROJECT)
    capabilities = tmp_path / "capabilities.py"
    capabilities.write_text(CAPABILITIES)
    command = [
        sys.executable,
        "-I",
        str(SCRIPT),
        "--site",
        str(site),
        "--releases",
        str(releases),
        "--project",
        str(project),
        "--capabilities",
        str(capabilities),
        "--repository",
        REPOSITORY,
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "Updated" in result.stdout
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert "already current" in result.stdout
    previous = site.read_bytes()
    releases.write_text("not json")
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert site.read_bytes() == previous


def test_workflow_release_hook_permissions_and_pages_repair():
    workflow = yaml.safe_load((ROOT / ".github/workflows/sync-release-site.yml").read_text())
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"workflow_call", "workflow_dispatch", "release"}
    assert triggers["release"]["types"] == ["published", "edited"]
    assert workflow["permissions"] == {"contents": "write", "pages": "write"}
    assert workflow["concurrency"] == {"group": "release-site-publish", "cancel-in-progress": False}
    steps = workflow["jobs"]["sync"]["steps"]
    checkouts = [
        step["with"] for step in steps if step.get("uses", "").startswith("actions/checkout@")
    ]
    assert checkouts == [
        {"ref": "main", "persist-credentials": False},
        {"ref": "gh-pages", "path": "site"},
    ]
    fetch = next(
        step["run"] for step in steps if step.get("name") == "Fetch published release facts"
    )
    assert "--paginate --slurp" in fetch
    assert "stable_releases" in fetch
    assert "?ref=${tag}" in fetch
    assert "base64 --decode" in fetch
    push = next(step for step in steps if step.get("name") == "Publish only generated site facts")
    assert push["working-directory"] == "site"
    assert "commit --only index.html" in push["run"]
    assert "git push origin HEAD:gh-pages" in push["run"]
    assert "--force" not in push["run"]
    poll = steps[-1]["run"]
    assert 'gh api --method POST "$builds"' in poll
    assert "git -C site rev-parse HEAD" in poll
    assert "for attempt in {1..60}" in poll
    assert 'endpoint="$pinned"' in poll
    assert steps[-1]["timeout-minutes"] == 8
    assert "if" not in steps[-1]
    release_workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    hook = release_workflow["jobs"]["site"]
    assert hook["needs"] == "release"
    assert hook["uses"] == "./.github/workflows/sync-release-site.yml"
    assert hook["permissions"] == workflow["permissions"]


@pytest.mark.skipif(
    sys.platform == "win32" or not shutil.which("jq"), reason="requires Linux bash and jq"
)
@pytest.mark.parametrize(
    "scenario,success",
    [
        ("built", True),
        ("unique", True),
        ("repository_id_post", True),
        ("repository_id_get", True),
        ("repository_id_stale", True),
        ("stale_then_built", True),
        ("wrong_commit", False),
        ("failed", False),
        ("timeout", False),
        ("old_timestamp", False),
        ("untrusted_post", False),
        ("untrusted_get", False),
        ("wrong_repo", False),
        ("wrong_repository_id", False),
        ("wrong_repository_id_get", False),
        ("identity_changed", False),
    ],
)
def test_pages_poll_executes_fail_closed(tmp_path, scenario, success):
    workflow = yaml.safe_load((ROOT / ".github/workflows/sync-release-site.yml").read_text())
    script = workflow["jobs"]["sync"]["steps"][-1]["run"]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    programs = {
        "git": '#!/bin/sh\nprintf "%040d\\n" 1\n',
        "date": "#!/bin/sh\necho 2026-09-08T12:00:00Z\n",
        "sleep": "#!/bin/sh\nexit 0\n",
        "timeout": '#!/bin/sh\nshift\nexec "$@"\n',
        "gh": """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
root = Path(os.environ["MOCK_ROOT"])
scenario = os.environ["SCENARIO"]
prefix = "https://api.github.com/repos/xibodev/brains-ai/pages/builds/"
id_prefix = "https://api.github.com/repositories/123/pages/builds/"
args = sys.argv[1:]
with (root / "calls").open("a") as handle:
    handle.write(json.dumps(args) + "\\n")
if "POST" in args:
    url = prefix + ("2" if scenario == "unique" else "latest")
    if scenario == "untrusted_post": url = "https://evil.test/builds/latest"
    if scenario == "wrong_repo": url = prefix.replace("xibodev", "other") + "latest"
    if scenario.startswith("repository_id_"): url = id_prefix + "latest"
    if scenario == "wrong_repository_id": url = id_prefix.replace("123", "456") + "latest"
    print(json.dumps({"url": url, "status": "queued"}))
elif "--jq" in args:
    print(json.dumps([(id_prefix if scenario == "repository_id_stale" else prefix) + "1"]))
else:
    count_path = root / "count"
    count = int(count_path.read_text()) + 1 if count_path.exists() else 1
    count_path.write_text(str(count))
    stale = scenario in ("stale_then_built", "repository_id_stale") and count == 1
    url = prefix + ("1" if stale else "2")
    if scenario == "repository_id_get": url = id_prefix + "2"
    if scenario == "wrong_repository_id_get": url = id_prefix.replace("123", "456") + "2"
    if scenario == "untrusted_get": url = "https://evil.test/builds/2"
    if scenario == "identity_changed" and count > 1: url = prefix + "3"
    status = "building" if count == 1 or scenario == "timeout" else "built"
    if scenario == "failed": status = "errored"
    print(json.dumps({"url": url, "status": status,
        "commit": "0" * 39 + ("2" if scenario == "wrong_commit" else "1"),
        "created_at": "2026-09-07T12:00:00Z" if scenario == "old_timestamp" or (stale and scenario != "repository_id_stale") else "2026-09-08T12:00:00Z"}))
""",
    }
    for name, source in programs.items():
        executable = bin_dir / name
        executable.write_text(source)
        executable.chmod(0o755)
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env={
            **os.environ,
            "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
            "MOCK_ROOT": str(tmp_path),
            "SCENARIO": scenario,
            "GITHUB_REPOSITORY": REPOSITORY,
            "GH_REPOSITORY_ID": "123",
        },
    )
    assert (result.returncode == 0) is success, result.stdout + result.stderr
    calls = [json.loads(line) for line in (tmp_path / "calls").read_text().splitlines()]
    assert all(
        not any("evil.test" in arg or "repos/other/" in arg for arg in call) for call in calls
    )
    assert all(not any("repositories/456/" in arg for arg in call) for call in calls)
    if success:
        assert "Pages built verified commit" in result.stdout
        assert calls[-1] == ["api", "repos/xibodev/brains-ai/pages/builds/2"]
    if scenario in ("timeout", "old_timestamp"):
        assert len(calls) == 62
        assert "Timed out" in result.stdout
