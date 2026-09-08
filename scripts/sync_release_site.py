"""Render marked static-site facts from GitHub Releases and matching tagged sources."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import tempfile
import tomllib
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import quote

STABLE_TAG = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def stable_releases(releases: object, repository: str) -> list[dict]:
    """Put the highest stable version first, then history by publication date."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("invalid repository")
    if not isinstance(releases, list):
        raise ValueError("releases must be a JSON list")
    stable = []
    seen = set()
    for release in releases:
        if not isinstance(release, dict):
            raise ValueError("invalid release record")
        if not isinstance(release.get("draft"), bool) or not isinstance(
            release.get("prerelease"), bool
        ):
            raise ValueError("missing release visibility flags")
        if release["draft"] or release["prerelease"]:
            continue
        tag = release.get("tag_name")
        if not isinstance(tag, str):
            raise ValueError("missing release tag")
        if not STABLE_TAG.fullmatch(tag):
            continue
        if tag in seen:
            raise ValueError("duplicate stable release tag")
        seen.add(tag)
        canonical = f"https://github.com/{repository}/releases/tag/{quote(tag, safe='')}"
        if release.get("html_url") != canonical:
            raise ValueError("release URL is not canonical")
        published = release.get("published_at")
        if not isinstance(published, str) or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", published
        ):
            raise ValueError("invalid release publication date")
        datetime.strptime(published, "%Y-%m-%dT%H:%M:%SZ")
        for field in ("name", "body"):
            if release.get(field) is not None and not isinstance(release[field], str):
                raise ValueError(f"invalid release {field}")
        stable.append(release)
    if not stable:
        raise ValueError("no published stable releases")
    stable.sort(key=lambda item: (item["published_at"], item["tag_name"]), reverse=True)
    latest = max(stable, key=lambda item: tuple(map(int, item["tag_name"][1:].split("."))))
    return [latest, *(item for item in stable if item is not latest)]


def tool_count(source: str) -> int:
    values = []
    for node in ast.parse(source).body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(
                isinstance(target, ast.Name) and target.id == "CORE_MCP_TOOLS" for target in targets
            ):
                values.append(node.value)
    if len(values) != 1:
        raise ValueError("expected one CORE_MCP_TOOLS assignment")
    value = values[0]
    if isinstance(value, ast.Call):
        if not (
            isinstance(value.func, ast.Name)
            and value.func.id == "frozenset"
            and len(value.args) == 1
            and not value.keywords
        ):
            raise ValueError("CORE_MCP_TOOLS must be a literal set or frozenset")
        value = value.args[0]
    if not isinstance(value, ast.Set):
        raise ValueError("CORE_MCP_TOOLS must contain a literal set")
    tools = ast.literal_eval(value)
    if not tools or any(not isinstance(tool, str) or not tool.strip() for tool in tools):
        raise ValueError("CORE_MCP_TOOLS must contain nonempty tool names")
    return len(tools)


def plain_text(text: str) -> str:
    # Deliberately not a Markdown renderer: no release-provided links or HTML execute.
    text = re.sub(r"!?\[([^\]]+)\]\([^\n]*?\)", r"\1", text)
    text = text.replace("`", "").replace("**", "").replace("__", "")
    return " ".join(text.split())


def highlights(body: str) -> list[str]:
    # Filter before finding headings: hidden examples must not select the section.
    visible = []
    fence = ""
    comment = False
    pending = ""
    for line in body.splitlines():
        if fence:
            if re.fullmatch(
                r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}[ \t]*", line
            ):
                fence = ""
            continue
        while True:
            if comment:
                end = line.find("-->")
                if end < 0:
                    break
                line = line[end + 3 :]
                comment = False
            line = pending + line
            pending = ""
            opening = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
            if opening and (opening[1][0] == "~" or "`" not in opening[2]):
                fence = opening[1]
                break
            start = line.find("<!--")
            if start < 0:
                visible.append(line)
                break
            pending = line[:start]
            line = line[start + 4 :]
            comment = True
    if pending:
        visible.append(pending)
    body = "\n".join(visible)
    section = re.search(r"^##[ \t]+Highlights[ \t]*\r?$", body, re.MULTILINE | re.IGNORECASE)
    lines = body[section.end() :].splitlines() if section else body.splitlines()
    result = []
    for line in lines:
        if re.match(r"^#{1,2}\s", line) and section:
            break
        line = line.strip()
        if not line or line.startswith(("#", "```", "<!--")):
            continue
        line = plain_text(re.sub(r"^(?:[-*+] |[0-9]+[.)] )", "", line))
        if not line or not re.search(r"\w", line):
            continue
        result.append(line[:500])
        if not section or len(result) == 6:
            break
    return result


def render_history(releases: list[dict]) -> str:
    articles = []
    for index, release in enumerate(releases[:6]):
        tag = release["tag_name"]
        url = escape(release["html_url"], quote=True)
        date = datetime.strptime(release["published_at"], "%Y-%m-%dT%H:%M:%SZ")
        label = f"{MONTHS[date.month - 1]} {date.day}, {date.year}"
        title = plain_text(release.get("name") or "")
        heading = f"<h4>{escape(title)}</h4>" if title and title not in (tag, tag[1:]) else ""
        items = highlights(release.get("body") or "")
        details = (
            "<ul>" + "".join(f"<li>{escape(item)}</li>" for item in items) + "</ul>"
            if items
            else ""
        )
        badge = '<span class="release-badge">Latest release</span>' if index == 0 else ""
        articles.append(
            f'<article class="release-entry" id="release-{tag.replace(".", "-")}">'
            f'<div class="release-meta"><h3><a href="{url}">{tag}</a></h3>'
            f'<time datetime="{date:%Y-%m-%d}">{label}</time>{badge}</div>'
            f'<div class="release-details">{heading}{details}'
            f'<a class="release-link" href="{url}">Full release notes</a></div></article>'
        )
    return "\n" + "\n".join(articles) + "\n"


def sync_site(
    site: Path, releases: object, project: str, capabilities: str, repository: str
) -> bool:
    stable = stable_releases(releases, repository)
    version = tomllib.loads(project).get("project", {}).get("version")
    if version != stable[0]["tag_name"][1:]:
        raise ValueError("latest stable release does not match tagged project version")
    replacements = {
        "release-history": render_history(stable),
        "release-version": version,
        "mcp-count": str(tool_count(capabilities)),
    }
    original = site.read_bytes()
    document = original.decode("utf-8")
    spans = []
    for name, replacement in replacements.items():
        start = f"<!-- brains:{name}:start -->"
        end = f"<!-- brains:{name}:end -->"
        count = document.count(start)
        if (
            count not in ((1,) if name == "release-history" else (1, 2))
            or document.count(end) != count
        ):
            raise ValueError(f"missing or duplicate {name} markers")
        matches = list(
            re.finditer(re.escape(start) + r"(.*?)" + re.escape(end), document, re.DOTALL)
        )
        if len(matches) != count:
            raise ValueError(f"malformed {name} markers")
        for match in matches:
            if "<!-- brains:" in match[1]:
                raise ValueError("nested release markers")
            spans.append((match.start(1), match.end(1), replacement))
    for span_start, span_end, replacement in sorted(spans, reverse=True):
        document = document[:span_start] + replacement + document[span_end:]
    rendered = document.encode("utf-8")
    if rendered == original:
        return False
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=site.parent, prefix=f".{site.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(site.stat().st_mode)
        os.replace(temporary, site)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", type=Path, required=True, help="existing marked index.html")
    parser.add_argument("--releases", type=Path, required=True, help="GitHub Releases JSON list")
    parser.add_argument(
        "--project", type=Path, required=True, help="latest stable tagged pyproject.toml"
    )
    parser.add_argument(
        "--capabilities", type=Path, required=True, help="latest stable tagged capabilities.py"
    )
    parser.add_argument("--repository", default="xibodev/brains-ai")
    args = parser.parse_args()
    try:
        changed = sync_site(
            args.site,
            json.loads(args.releases.read_text(encoding="utf-8")),
            args.project.read_text(encoding="utf-8"),
            args.capabilities.read_text(encoding="utf-8"),
            args.repository,
        )
    except (ValueError, SyntaxError, OSError) as exc:
        parser.exit(1, f"release site sync failed: {exc}\n")
    print("Updated release site" if changed else "Release site already current")


if __name__ == "__main__":
    main()
