"""Render marked static-site facts from GitHub Releases and matching tagged sources."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import stat
import tempfile
import tomllib
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import quote

STABLE_TAG = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
SITE_PAGES = ("index.html", "quickstart.html", "mcp.html", "releases.html")


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


def render_summary(release: dict) -> str:
    tag = release["tag_name"]
    date = datetime.strptime(release["published_at"], "%Y-%m-%dT%H:%M:%SZ")
    label = f"{MONTHS[date.month - 1]} {date.day}, {date.year}"
    items = highlights(release.get("body") or "")[:2]
    details = (
        "<ul>" + "".join(f"<li>{escape(item)}</li>" for item in items) + "</ul>" if items else ""
    )
    return (
        '\n<div class="release-summary"><p class="release-summary-meta">'
        f'<a href="releases.html#release-{escape(tag.replace(".", "-"), quote=True)}">'
        f"Latest release: {escape(tag)}</a> "
        f'<time datetime="{date:%Y-%m-%d}">{label}</time></p>{details}'
        '<a class="release-link" href="releases.html">All release notes</a></div>\n'
    )


def reject_link(path: Path) -> None:
    info = path.lstat()
    if (
        stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise ValueError(f"symlink or reparse point is not allowed: {path}")


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
    site = site.absolute()
    for path in (*reversed(site.parents), site):
        reject_link(path)
    directory = site.is_dir()
    originals = {}
    if directory:
        for path in sorted(site.iterdir()):
            reject_link(path)
            if path.suffix.lower() != ".html":
                continue
            if not path.is_file():
                raise ValueError(f"HTML page is not a regular file: {path.name}")
            original = path.read_bytes()
            document = original.decode("utf-8")
            if path.name not in SITE_PAGES and "<!-- brains:" in document:
                raise ValueError(f"release markers in unsupported page: {path.name}")
            originals[path.name] = original
        legacy = set(originals) == {"index.html"} and "<!-- brains:release-history:" in originals[
            "index.html"
        ].decode("utf-8")
        if not legacy and not {"index.html", "releases.html"} <= originals.keys():
            raise ValueError("directory requires index.html and releases.html")
    else:
        legacy = True
        originals[site.name] = site.read_bytes()
    if not legacy:
        replacements["release-summary"] = render_summary(stable[0])
    totals = dict.fromkeys(replacements, 0)
    outputs = {}
    for filename, original in originals.items():
        if directory and filename not in SITE_PAGES:
            continue
        document = original.decode("utf-8")
        spans = []
        for name, replacement in replacements.items():
            start = f"<!-- brains:{name}:start -->"
            end = f"<!-- brains:{name}:end -->"
            count = document.count(start)
            if legacy:
                allowed: tuple[int, ...] = (1,) if name == "release-history" else (1, 2)
            elif name in ("release-version", "mcp-count"):
                allowed = tuple(range(5))
            else:
                owner = "index.html" if name == "release-summary" else "releases.html"
                allowed = (1,) if filename == owner else (0,)
            if count not in allowed or document.count(end) != count:
                raise ValueError(f"missing or duplicate {name} markers in {filename}")
            matches = list(
                re.finditer(re.escape(start) + r"(.*?)" + re.escape(end), document, re.DOTALL)
            )
            if len(matches) != count:
                raise ValueError(f"malformed {name} markers in {filename}")
            for match in matches:
                if "<!-- brains:" in match[1]:
                    raise ValueError("nested release markers")
                spans.append((match.start(1), match.end(1), replacement))
            totals[name] += count
        # Reject stray, misspelled, or legacy-incompatible marker tokens as well.
        tokens = re.findall(r"<!-- brains:[^>]*-->", document)
        expected = sum(document.count(f"<!-- brains:{name}:start -->") for name in replacements)
        if len(tokens) != 2 * expected or document.count("<!-- brains:") != len(tokens):
            raise ValueError(f"unsupported or malformed release markers in {filename}")
        for span_start, span_end, replacement in sorted(spans, reverse=True):
            document = document[:span_start] + replacement + document[span_end:]
        rendered = document.encode("utf-8")
        if rendered != original:
            outputs[site / filename if directory else site] = rendered
    if not totals["release-version"] or not totals["mcp-count"]:
        raise ValueError("site requires release-version and mcp-count markers")

    # Validate everything, then stage everything before replacing any page. Replaces
    # are atomic per file, not a filesystem-wide transaction; publication is gated by exit 0.
    staged = {}
    try:
        for path, rendered in outputs.items():
            with tempfile.NamedTemporaryFile(
                dir=path.parent, prefix=f".{path.name}.", delete=False
            ) as handle:
                temporary = Path(handle.name)
                staged[path] = temporary
                handle.write(rendered)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(path.stat().st_mode)
        for path, temporary in staged.items():
            os.replace(temporary, path)
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)
    return bool(outputs)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--site", type=Path, required=True, help="static site directory or legacy marked index.html"
    )
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
