from __future__ import annotations

import importlib.util
import json
import os
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


def directory_pages(site: Path) -> dict[str, bytes]:
    documents = {
        "index.html": marker("release-summary") + marker("release-version"),
        "quickstart.html": marker("release-version") * 4,
        "mcp.html": marker("mcp-count") * 4,
        "releases.html": marker("release-history"),
        "about.html": "<p>Unmanaged page</p>",
    }
    originals = {}
    for name, content in documents.items():
        originals[name] = ("<!doctype html>\r\n" + content + "\r\n<footer>Keep</footer>").encode()
        (site / name).write_bytes(originals[name])
    return originals


def test_directory_sync_preserves_unmarked_bytes_and_mtimes(tmp_path):
    originals = directory_pages(tmp_path)
    assert sync.sync_site(tmp_path, [release()], PROJECT, CAPABILITIES, REPOSITORY)
    replacements = {
        "release-summary": sync.render_summary(release()),
        "release-history": sync.render_history([release()]),
        "release-version": "1.5.0",
        "mcp-count": "2",
    }
    for name, original in originals.items():
        expected = original
        for key, value in replacements.items():
            expected = expected.replace(marker(key).encode(), marker(key, value).encode())
        assert (tmp_path / name).read_bytes() == expected
    mtimes = {path.name: path.stat().st_mtime_ns for path in tmp_path.iterdir()}
    assert not sync.sync_site(tmp_path, [release()], PROJECT, CAPABILITIES, REPOSITORY)
    assert {path.name: path.stat().st_mtime_ns for path in tmp_path.iterdir()} == mtimes


def test_directory_legacy_fallback_matches_single_file(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_bytes(page())
    single = tmp_path / "index.html"
    single.write_bytes(page())
    assert sync.sync_site(site, [release()], PROJECT, CAPABILITIES, REPOSITORY)
    assert sync.sync_site(single, [release()], PROJECT, CAPABILITIES, REPOSITORY)
    assert (site / "index.html").read_bytes() == single.read_bytes()
    modified = (site / "index.html").stat().st_mtime_ns
    assert not sync.sync_site(site, [release()], PROJECT, CAPABILITIES, REPOSITORY)
    assert (site / "index.html").stat().st_mtime_ns == modified


@pytest.mark.parametrize("optional_pages", [False, True])
def test_directory_optional_pages_can_be_absent_or_unmarked(tmp_path, optional_pages):
    (tmp_path / "index.html").write_text(marker("release-summary") + marker("release-version"))
    (tmp_path / "releases.html").write_text(marker("release-history") + marker("mcp-count"))
    mtimes = {}
    if optional_pages:
        for name in ("quickstart.html", "mcp.html"):
            path = tmp_path / name
            path.write_text("<p>Static search content</p>")
            mtimes[name] = path.stat().st_mtime_ns
    assert sync.sync_site(tmp_path, [release()], PROJECT, CAPABILITIES, REPOSITORY)
    for name, modified in mtimes.items():
        assert (tmp_path / name).read_text() == "<p>Static search content</p>"
        assert (tmp_path / name).stat().st_mtime_ns == modified


@pytest.mark.parametrize(
    "filename,content",
    [
        ("index.html", marker("release-version")),
        ("index.html", marker("release-summary") * 2),
        ("releases.html", "missing history"),
        ("releases.html", marker("release-history") * 2),
        ("mcp.html", marker("mcp-count") * 5),
        ("quickstart.html", marker("release-version") * 5),
        ("mcp.html", marker("mcp-count") + marker("release-history")),
        ("releases.html", marker("release-history") + marker("release-summary")),
        ("mcp.html", marker("mcp-count", marker("release-version"))),
        ("mcp.html", "<!-- brains:mcp-count:end --><!-- brains:mcp-count:start -->"),
        ("mcp.html", marker("mcp-count") + "<!-- brains:mcp-count:end -->"),
        ("mcp.html", marker("mcp-count") + marker("release-typo")),
        ("mcp.html", marker("mcp-count") + "<!-- brains:release-version:start"),
        ("about.html", marker("release-version")),
        ("extra.HTML", marker("release-summary")),
    ],
)
def test_directory_invalid_markers_leave_every_page_untouched(tmp_path, filename, content):
    originals = directory_pages(tmp_path)
    originals[filename] = content.encode()
    (tmp_path / filename).write_bytes(originals[filename])
    mtimes = {path.name: path.stat().st_mtime_ns for path in tmp_path.iterdir()}
    with pytest.raises(ValueError):
        sync.sync_site(tmp_path, [release()], PROJECT, CAPABILITIES, REPOSITORY)
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == originals
    assert {path.name: path.stat().st_mtime_ns for path in tmp_path.iterdir()} == mtimes


@pytest.mark.parametrize("missing", ["release-version", "mcp-count", "index.html", "releases.html"])
def test_directory_required_pages_and_global_facts(tmp_path, missing):
    originals = directory_pages(tmp_path)
    if missing.endswith(".html"):
        (tmp_path / missing).unlink()
        del originals[missing]
    else:
        for name, original in originals.items():
            originals[name] = original.replace(marker(missing).encode(), b"")
            (tmp_path / name).write_bytes(originals[name])
    with pytest.raises(ValueError):
        sync.sync_site(tmp_path, [release()], PROJECT, CAPABILITIES, REPOSITORY)
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == originals


@pytest.mark.parametrize(
    "records,project,capabilities",
    [([], PROJECT, CAPABILITIES), ([release()], "", CAPABILITIES), ([release()], PROJECT, "")],
)
def test_directory_invalid_metadata_preserves_all_pages(tmp_path, records, project, capabilities):
    originals = directory_pages(tmp_path)
    with pytest.raises(ValueError):
        sync.sync_site(tmp_path, records, project, capabilities, REPOSITORY)
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == originals


@pytest.mark.parametrize(
    "target", ["root", "ancestor", "index.html", "about.html", "assets", "broken"]
)
def test_directory_rejects_symlinks_without_outside_writes(tmp_path, target):
    site = tmp_path / "site"
    site.mkdir()
    originals = directory_pages(site)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "index.html"
    sentinel.write_bytes(page())
    link = tmp_path / "linked" if target in ("root", "ancestor") else site / target
    destination = (
        site if target in ("root", "ancestor") else outside if target == "assets" else sentinel
    )
    if target == "broken":
        destination = outside / "missing"
    if link.exists():
        link.unlink()
    try:
        link.symlink_to(destination, target_is_directory=destination.is_dir())
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    argument = link if target == "root" else link / "index.html" if target == "ancestor" else site
    with pytest.raises(ValueError, match="symlink"):
        sync.sync_site(argument, [release()], PROJECT, CAPABILITIES, REPOSITORY)
    assert sentinel.read_bytes() == page()
    for name, original in originals.items():
        if name != target:
            assert (site / name).read_bytes() == original
    assert link.is_symlink()


def test_directory_stages_all_outputs_before_replacing(tmp_path, monkeypatch):
    originals = directory_pages(tmp_path)
    real_fsync = sync.os.fsync
    calls = 0

    def fail_second_stage(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("staging failed")
        real_fsync(fd)

    monkeypatch.setattr(sync.os, "fsync", fail_second_stage)
    with pytest.raises(OSError, match="staging failed"):
        sync.sync_site(tmp_path, [release()], PROJECT, CAPABILITIES, REPOSITORY)
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == originals


def test_directory_replace_failure_cleans_staged_files(tmp_path, monkeypatch):
    originals = directory_pages(tmp_path)
    real_replace = sync.os.replace
    calls = 0

    def fail_second_replace(source, destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("replacement failed")
        real_replace(source, destination)

    monkeypatch.setattr(sync.os, "replace", fail_second_replace)
    with pytest.raises(OSError, match="replacement failed"):
        sync.sync_site(tmp_path, [release()], PROJECT, CAPABILITIES, REPOSITORY)
    assert {path.name for path in tmp_path.iterdir()} == set(originals)
    # Per-file replacement can leave local partial output. A retry converges;
    # the workflow must never commit after this nonzero generator result.
    monkeypatch.setattr(sync.os, "replace", real_replace)
    assert sync.sync_site(tmp_path, [release()], PROJECT, CAPABILITIES, REPOSITORY)
    assert not sync.sync_site(tmp_path, [release()], PROJECT, CAPABILITIES, REPOSITORY)


def test_summary_contract_first_two_highlights_and_escaping():
    html = sync.render_summary(
        release(body='## Highlights\n- <img onerror="bad()">\n- ' + "x" * 600 + "\n- Third")
    )
    assert '<div class="release-summary"><p class="release-summary-meta">' in html
    assert '<a href="releases.html#release-v1-5-0">Latest release: v1.5.0</a>' in html
    assert '<time datetime="2026-09-08">Sep 8, 2026</time>' in html
    assert "<li>&lt;img onerror=&quot;bad()&quot;&gt;</li>" in html
    assert "<li>" + "x" * 500 + "</li>" in html
    assert html.count("<li>") == 2
    assert "Third" not in html
    assert '<a class="release-link" href="releases.html">All release notes</a>' in html
    assert "<ul>" not in sync.render_summary(release(body=None))


@pytest.mark.parametrize("mode", ["file", "legacy-directory", "directory"])
def test_cli_end_to_end_and_malformed_json(tmp_path, mode):
    site = tmp_path / "site"
    site.mkdir()
    if mode == "directory":
        directory_pages(site)
    else:
        (site / "index.html").write_bytes(page())
    argument = site / "index.html" if mode == "file" else site
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
        str(argument),
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
    previous = {path.name: path.read_bytes() for path in site.iterdir()}
    releases.write_text("not json")
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert {path.name: path.read_bytes() for path in site.iterdir()} == previous


def test_workflow_release_hook_permissions_and_pages_repair():
    workflow = yaml.safe_load((ROOT / ".github/workflows/sync-release-site.yml").read_text())
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"workflow_call", "workflow_dispatch", "release"}
    assert triggers["release"]["types"] == ["published", "edited"]
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {"group": "pages", "cancel-in-progress": False}
    assert workflow["jobs"]["sync"]["permissions"] == {"contents": "write", "pages": "read"}
    assert workflow["jobs"]["sync"]["if"] == (
        "github.ref == 'refs/heads/main' || github.event_name == 'release' || "
        "(github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v'))"
    )
    steps = workflow["jobs"]["sync"]["steps"]
    checkouts = [
        step["with"] for step in steps if step.get("uses", "").startswith("actions/checkout@")
    ]
    assert checkouts == [{"ref": "main"}]
    fetch = next(
        step["run"] for step in steps if step.get("name") == "Fetch published release facts"
    )
    assert "--paginate --slurp" in fetch
    assert "stable_releases" in fetch
    assert "?ref=${tag}" in fetch
    assert "base64 --decode" in fetch
    push = next(step for step in steps if step.get("name") == "Publish only generated site facts")
    assert "working-directory" not in push
    generate = next(step for step in steps if step.get("name") == "Generate static release history")
    assert "--site site \\\n" in generate["run"]
    assert (
        "git ls-files -- site/index.html site/quickstart.html site/mcp.html site/releases.html"
        in push["run"]
    )
    assert 'mapfile -t pages <<< "$tracked"' in push["run"]
    assert 'git diff --quiet -- "${pages[@]}"' in push["run"]
    assert "commit --only -m" in push["run"]
    assert '-- "${pages[@]}"' in push["run"]
    assert "git push origin HEAD:main" in push["run"]
    assert "--force" not in push["run"]
    assert steps[-2]["uses"] == "actions/configure-pages@v5"
    assert "enablement" not in steps[-2].get("with", {})
    assert steps[-1]["uses"] == "actions/upload-pages-artifact@v3"
    assert steps[-1]["with"] == {"path": "site"}
    assert "if" not in steps[-1]
    release_workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    hook = release_workflow["jobs"]["site"]
    assert hook["needs"] == "release"
    assert hook["uses"] == "./.github/workflows/sync-release-site.yml"
    assert hook["permissions"] == {"contents": "write", "pages": "write", "id-token": "write"}


def test_site_deploy_uses_trusted_main_and_serialized_artifact_deployment():
    workflow = yaml.safe_load((ROOT / ".github/workflows/site-deploy.yml").read_text())
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"push", "workflow_dispatch"}
    assert triggers["push"] == {
        "branches": ["main"],
        "paths": ["site/**", ".github/workflows/site-deploy.yml"],
    }
    assert workflow["permissions"] == {"contents": "read"}
    build = workflow["jobs"]["build"]
    assert build["if"] == "github.ref == 'refs/heads/main'"
    assert build["permissions"] == {"contents": "read", "pages": "read"}
    assert build["steps"] == [
        {"uses": "actions/checkout@v4", "with": {"ref": "main", "persist-credentials": False}},
        {"uses": "actions/configure-pages@v5"},
        {"uses": "actions/upload-pages-artifact@v3", "with": {"path": "site"}},
    ]
    for filename, dependency in [("site-deploy.yml", "build"), ("sync-release-site.yml", "sync")]:
        publisher = yaml.safe_load((ROOT / ".github/workflows" / filename).read_text())
        assert publisher["concurrency"] == {"group": "pages", "cancel-in-progress": False}
        deploy = publisher["jobs"]["deploy"]
        assert deploy["needs"] == dependency
        assert "if" not in deploy
        assert deploy["permissions"] == {"pages": "write", "id-token": "write"}
        assert deploy["environment"] == {
            "name": "github-pages",
            "url": "${{ steps.deployment.outputs.page_url }}",
        }
        assert deploy["steps"] == [
            {"name": "Deploy GitHub Pages", "id": "deployment", "uses": "actions/deploy-pages@v4"}
        ]


@pytest.mark.skipif(sys.platform == "win32", reason="requires Linux bash")
@pytest.mark.parametrize("modern", [False, True])
@pytest.mark.parametrize("changed", [False, True])
def test_publish_step_uses_only_tracked_allowlisted_paths(tmp_path, modern, changed):
    workflow = yaml.safe_load((ROOT / ".github/workflows/sync-release-site.yml").read_text())
    script = next(
        step["run"]
        for step in workflow["jobs"]["sync"]["steps"]
        if step.get("name") == "Publish only generated site facts"
    )
    executable = tmp_path / "git"
    executable.write_text("""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with Path(os.environ["CALLS"]).open("a") as handle:
    handle.write(json.dumps(args) + "\\n")
if args[0] == "ls-files":
    print(os.environ["TRACKED"])
elif args[0] == "diff":
    sys.exit(int(os.environ["CHANGED"]))
""")
    executable.chmod(0o755)
    pages = [f"site/{name}" for name in sync.SITE_PAGES] if modern else ["site/index.html"]
    calls_path = tmp_path / "calls"
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "CALLS": str(calls_path),
            "TRACKED": "\n".join(pages),
            "CHANGED": str(int(changed)),
        },
    )
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
    assert calls[0] == ["ls-files", "--", *(f"site/{name}" for name in sync.SITE_PAGES)]
    assert calls[1] == ["diff", "--quiet", "--", *pages]
    if changed:
        assert len(calls) == 4
        assert calls[2][4:] == [
            "commit",
            "--only",
            "-m",
            "docs: sync published release site",
            "--",
            *pages,
        ]
        assert calls[3] == ["push", "origin", "HEAD:main"]
    else:
        assert len(calls) == 2
