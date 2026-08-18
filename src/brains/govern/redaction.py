"""Canonical secret redaction for everything a governed action records.

One command line reaches four places: the normalised argument vector that the
approval digest is computed over, the summary stored on ``governed_actions``,
the audit payload, the ASK body an operator reads, and the bridge message that
ASK is relayed through. A secret that survives into any one of them is
persisted forever in a hash-chained log that is deliberately hard to rewrite,
so redaction has to happen *before* the value reaches any of them - which is
why it lives here, in one place, and is applied at the
:class:`~brains.govern.GovernedRequest` boundary rather than at each sink.

What is redacted
----------------

* **URL credentials** - ``https://user:token@host`` and ``redis://:pw@host``
  lose the password; a userinfo that is itself a known token shape goes too.
* **``NAME=VALUE`` with a secret-shaped name**, with or without a flag prefix,
  so ``--token=x``, ``GITHUB_TOKEN=x``, ``docker run -e API_KEY=x`` and a
  ``set TOKEN=x`` line all normalise the same way. A name is secret-shaped
  when it contains a distinctive word (``token``, ``password``, ``api_key``)
  *or* when a whole segment of it is one of the bare words that only mean a
  credential on their own - ``DB_PASS``, ``MASTER_KEY``, ``dbPass`` - which
  ``bypass``, ``passenger``, ``keyboard`` and ``monkey`` are not. Quotes are
  stripped from the match so the Windows/PowerShell spellings behave like the
  POSIX ones.
* **Header values** - ``-H "Authorization: Bearer x"``, ``Cookie:``,
  ``X-Api-Key:`` and friends, matched on the header *name* so an ordinary
  ``-H "Accept: application/json"`` is left intact.
* **Credential flags** - ``curl -u user:pass`` / ``--user``, ``--password``,
  and any flag whose own name is *unambiguously* a credential (``--token``,
  ``--db-pass``, ``-MasterKey``), whose value is the next argument. Short
  forms are read per tool, and per subcommand where that is what decides:
  ``curl -b SESSION=...``, ``redis-cli -a pw``, ``sshpass -p pw``,
  ``mongosh -p pw`` and ``docker login -p pw`` are credentials, while
  ``docker run -p 8080:80``, ``ssh -p 2222``, ``python -u`` and
  ``wget -b`` (background) keep their meaning. A lone
  bare word (``--key``, ``--cred``) is not: ``aws s3api get-object --key
  prod/backup.tar.gz`` names the resource an operator is approving, and
  redacting it would make every object in a bucket share one digest. Such a
  flag still redacts its own ``--key=VALUE`` form, and a secret-shaped value
  is redacted by shape wherever it appears.
* **Request bodies** - ``-d``/``--data``/``--data-raw``/``--data-binary`` are
  scanned for secret-named fields rather than redacted whole, so
  ``-d 'user=me&password=x'`` keeps the part that identifies the command.
* **Known token shapes** - GitHub/GitLab/Slack/OpenAI/Anthropic/AWS/Google/npm
  /HuggingFace/Docker/SendGrid prefixes, JWTs, and PEM private-key headers.
* **High-confidence generic tokens** - 32+ characters mixing upper, lower and
  digits. A 40-character git SHA (lowercase hex) and an ordinary path do not
  match, which is the point: over-redacting the arguments would destroy the
  approval digest's ability to distinguish one command from another.

The digest therefore binds the *redacted, normalised shape* of a command: two
invocations that differ only in their secret hash identically, and neither
hash depends on a value that is stored anywhere.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

REDACTED = "<redacted>"

#: Substrings that make an argument *name* (or header name, or form field)
#: secret-bearing. Matched case-insensitively against the name with leading
#: dashes stripped, so ``--api-key``, ``API_KEY`` and ``X-Api-Key`` all hit.
SECRET_NAME_PARTS: tuple[str, ...] = (
    "token",
    "secret",
    "password",
    "passwd",
    "passphrase",
    "pwd",
    "api-key",
    "api_key",
    "apikey",
    "access-key",
    "access_key",
    "accesskey",
    "authorization",
    "auth-token",
    "auth_token",
    "bearer",
    "cookie",
    "credential",
    "private-key",
    "private_key",
    "privatekey",
    "session-key",
    "session_key",
    "client-secret",
    "client_secret",
    "signing-key",
    "signing_key",
)

#: Words that are credentials when they are a *whole segment* of a name, and
#: ordinary English otherwise. ``DB_PASS``, ``--db-pass``, ``dbPass`` and a
#: JSON ``"pass"`` field are passwords; ``MASTER_KEY`` and ``signingKey`` are
#: keys. Substring matching cannot express that - it would redact the value of
#: ``bypass``, ``passenger_name``, ``keyboard_layout`` and ``monkey_patch``,
#: which is not merely noisy: an over-redacted argument stops identifying the
#: command an operator is approving. Segments are therefore matched whole,
#: split on separators and at camelCase boundaries.
SECRET_NAME_SEGMENTS: frozenset[str] = frozenset(
    {
        "pass",
        "passes",
        "key",
        "keys",
        "keyfile",
        "keystore",
        "cred",
        "creds",
    }
)

#: Splits a name into its segments: separators first, then camelCase, then
#: letter/digit boundaries, so ``dbPass``, ``DB_PASS``, ``db.pass`` and
#: ``KEY2`` all yield the segment they mean.
_NAME_SEGMENT = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]*|[a-z]+|[0-9]+")


def _segments(name: str) -> list[str]:
    """The words a name is made of, with any flag prefix and quotes removed."""
    return _NAME_SEGMENT.findall(name.strip().strip(_QUOTES).lstrip("-"))


#: Flags whose *next* argument is a credential even though the flag name
#: carries no secret-shaped word. The unambiguous long forms apply to every
#: tool; the short forms are so overloaded (``python -u``, ``sort -u``,
#: ``ssh -p``, ``docker run -p``, ``wget -b`` = run in the background) that
#: they are only read as credentials for the tools - and, where the short flag
#: only means a credential under one subcommand, the *subcommands* - where that
#: is what they mean.
_CREDENTIAL_FLAGS = frozenset({"--user", "--password", "--pass", "--pw"})
_TOOL_CREDENTIAL_FLAGS: dict[str, frozenset[str]] = {
    # ``curl -b`` is a cookie (a session credential) or the jar it is read
    # from; ``wget -b`` is ``--background`` and takes no value at all, so
    # wget's cookie handling is left to its long ``--load-cookies`` form,
    # whose name is already secret-shaped.
    "curl": frozenset({"-u", "-b"}),
    "wget": frozenset({"-u"}),
    "http": frozenset({"-a"}),
    "httpie": frozenset({"-a"}),
    "mysql": frozenset({"-p"}),
    "mysqladmin": frozenset({"-p"}),
    "mysqldump": frozenset({"-p"}),
    "mariadb": frozenset({"-p"}),
    "mariadb-dump": frozenset({"-p"}),
    "redis-cli": frozenset({"-a"}),
    "mongo": frozenset({"-p"}),
    "mongosh": frozenset({"-p"}),
    "mongodump": frozenset({"-p"}),
    "mongorestore": frozenset({"-p"}),
    "sshpass": frozenset({"-p"}),
    "mosquitto_pub": frozenset({"-P"}),
    "mosquitto_sub": frozenset({"-P"}),
}

#: Short flags that only mean a credential under one subcommand of a tool.
#: ``docker login -p hunter2`` is a registry password; ``docker run -p
#: 8080:80`` is a published port, and redacting it would erase what the
#: operator is being asked to approve. The subcommand is therefore part of the
#: key, and the widening only happens once that subcommand is actually seen.
_TOOL_SUBCOMMAND_CREDENTIAL_FLAGS: dict[str, dict[str, frozenset[str]]] = {
    "docker": {"login": frozenset({"-p"})},
    "podman": {"login": frozenset({"-p"})},
    "nerdctl": {"login": frozenset({"-p"})},
    "buildah": {"login": frozenset({"-p"})},
    "skopeo": {"login": frozenset({"-p"})},
    "helm": {"login": frozenset({"-p"})},
}

#: Flags whose next argument is a header line; the header name decides.
#: ``--oauth2-bearer`` deliberately is *not* here: its operand is a bare token
#: with no ``Name: Value`` shape, so it is redacted whole by its own name.
_HEADER_FLAGS = frozenset({"-H", "--header"})

#: Flags whose next argument is a request body; secret-named fields inside it
#: are redacted, the rest of the body is kept.
_BODY_FLAGS = frozenset(
    {
        "-d",
        "--data",
        "--data-raw",
        "--data-binary",
        "--data-urlencode",
        "--form",
        "-F",
        "--json",
    }
)

#: Flags that carry a ``NAME=VALUE`` pair in their next argument.
_ASSIGNMENT_FLAGS = frozenset({"-e", "--env", "--build-arg", "--set", "--setenv"})

_QUOTES = "\"'"

_KNOWN_TOKEN = re.compile(
    r"""(?x)
      \bgh[pousr]_[A-Za-z0-9]{16,}
    | \bgithub_pat_[A-Za-z0-9_]{20,}
    | \bglpat-[A-Za-z0-9_\-]{16,}
    | \bxox[abposr]-[A-Za-z0-9-]{10,}
    | \bsk-(?:ant-)?[A-Za-z0-9_\-]{20,}
    | \b(?:AKIA|ASIA)[0-9A-Z]{16}\b
    | \bAIza[0-9A-Za-z_\-]{30,}
    | \bnpm_[A-Za-z0-9]{30,}
    | \bhf_[A-Za-z0-9]{20,}
    | \bdckr_pat_[A-Za-z0-9_\-]{16,}
    | \bSG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}
    | \beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}
    | -----BEGIN[A-Z ]*PRIVATE KEY-----
    """
)

#: A generic token shape confident enough to redact: long, and mixing all
#: three character classes. Deliberately excludes lowercase hex (git SHAs),
#: pure uppercase constants, and anything with a path separator - a path or an
#: ``s3://`` URI must survive, because it is what identifies the command an
#: operator is being asked to approve.
_GENERIC_TOKEN = re.compile(r"[A-Za-z0-9_\-+=]{32,}")

#: URL userinfo. Every run is length-bounded so a long argument that contains
#: no URL cannot make the scan quadratic: the gate redacts *before* it records,
#: so a slow redaction is a slow governed action.
_URL_CREDENTIALS = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]{0,15}://)"
    r"(?P<user>[^\s/:@]{0,256})(?::(?P<password>[^\s/@]{0,512}))?@"
)

#: ``NAME=VALUE`` / ``NAME: VALUE`` / ``"NAME": "VALUE"`` where NAME is
#: secret-shaped. The delimiter in front of the name is a zero-width
#: lookbehind, so a nested assignment (``--env=API_KEY=x``) is matchable at any
#: offset, and the name is bounded, so a long opaque argument cannot make the
#: scan retry the whole run at every position. The value stops at whitespace, a
#: quote, or an argument/query separator, so one assignment inside a body or a
#: URL is redacted without eating the rest - except after an auth scheme word
#: (``Bearer x``), where the token *is* the following word and stopping at the
#: space would leave it.
_ASSIGNMENT = re.compile(
    r"(?:(?<=[\s,;&?\"'({\[=:])|^)"
    r"(?P<flag>-{0,2})(?P<name>[A-Za-z_][A-Za-z0-9_.\-]{0,63})"
    r"(?P<sep>[\"']?\s*[=:]\s*)"
    r"(?P<value>\"[^\"]*\"|'[^']*'"
    r"|(?:[Bb]earer|[Bb]asic|[Dd]igest|[Tt]oken|JWT)[ \t]+[^\s&;,\"']+"
    r"|[^\s&;,\"']+)"
)


def is_secret_name(name: str) -> bool:
    """True when an argument, header or field name identifies a credential.

    Two rules, because names are spelled two ways. A distinctive word
    (``token``, ``password``, ``api_key``) is matched anywhere in the name, so
    a prefix or a vendor spelling cannot hide it. A word that is only a
    credential when it stands alone (``pass``, ``key``) is matched as a whole
    *segment* - ``DB_PASS``, ``--db-pass``, ``dbPass``, ``MASTER_KEY`` - so
    ``bypass``, ``passenger``, ``keyboard`` and ``monkey`` keep their values.

    This is the test for a *name bound to its own value*: ``NAME=VALUE``, a
    header, a body field, a mapping entry. For a bare flag whose value is the
    *next* argument, use :func:`is_credential_flag_name`, which is stricter.
    """
    cleaned = name.strip().strip(_QUOTES).lstrip("-").lower()
    if any(part in cleaned for part in SECRET_NAME_PARTS):
        return True
    return any(segment.lower() in SECRET_NAME_SEGMENTS for segment in _segments(name))


def is_credential_flag_name(name: str) -> bool:
    """True when a flag name means "the *next* argument is a credential".

    Stricter than :func:`is_secret_name`, because a lone bare word says less
    about the argument after it than it does about a value bound to it. ``aws
    s3api get-object --key prod/backup.tar.gz`` names an object, ``docker
    manifest --key`` names a file, and redacting those turned the resource an
    operator is approving into ``<redacted>`` - and made every object in a
    bucket share one approval digest. So a lone ``--key``/``--pass``/``--cred``
    only qualifies its own ``NAME=VALUE`` value, while a qualified name
    (``--db-pass``, ``MASTER_KEY``, ``-DbPass``) or a distinctive word
    (``--token``, ``--api-key``, ``--client-secret``) still claims the next
    argument. A value that is itself secret-shaped is redacted anyway, by
    shape, wherever it appears.
    """
    cleaned = name.strip().strip(_QUOTES).lstrip("-").lower()
    if any(part in cleaned for part in SECRET_NAME_PARTS):
        return True
    segments = [segment for segment in _segments(name) if not segment.isdigit()]
    return len(segments) > 1 and any(
        segment.lower() in SECRET_NAME_SEGMENTS for segment in segments
    )


def _mixed_classes(value: str) -> bool:
    return (
        any(char.islower() for char in value)
        and any(char.isupper() for char in value)
        and any(char.isdigit() for char in value)
    )


def _redact_generic(match: re.Match[str]) -> str:
    value = match.group(0)
    return REDACTED if _mixed_classes(value) else value


def _redact_url_credentials(match: re.Match[str]) -> str:
    scheme = match.group("scheme")
    user = match.group("user") or ""
    password = match.group("password")
    if password is not None:
        return f"{scheme}{user}:{REDACTED}@"
    if user and (_KNOWN_TOKEN.search(user) or (len(user) >= 32 and _mixed_classes(user))):
        return f"{scheme}{REDACTED}@"
    return match.group(0)


def _redact_assignments(text: str) -> str:
    """Redact every secret-named assignment, including nested ones.

    A plain ``re.sub`` would let an *innocent* assignment swallow a secret one:
    in ``command failed: PGPASSWORD=hunter2 psql`` or ``--env=API_KEY=hunter2``,
    the outer match consumes the inner assignment as its value and substitution
    would resume past the secret it just ate. Scanning resumes at the nested
    value instead, which the zero-width lead lookbehind makes matchable.
    """
    out: list[str] = []
    position = 0
    while position < len(text):
        match = _ASSIGNMENT.search(text, position)
        if match is None:
            out.append(text[position:])
            return "".join(out)
        if is_secret_name(match.group("name")):
            out.append(text[position : match.start()])
            out.append(f"{match.group('flag')}{match.group('name')}{match.group('sep')}{REDACTED}")
            position = match.end()
            continue
        resume = max(match.start("value"), position + 1)
        out.append(text[position:resume])
        position = resume
    return "".join(out)


def redact_text(text: str) -> str:
    """Redact every credential shape this module knows about in free text.

    Used for summaries, ASK bodies and bridge messages as well as for
    individual arguments, so one command has one redacted spelling wherever it
    is recorded. Idempotent: redacting an already-redacted string is a no-op.
    """
    if not text:
        return ""
    out = _URL_CREDENTIALS.sub(_redact_url_credentials, str(text))
    out = _redact_assignments(out)
    out = _KNOWN_TOKEN.sub(REDACTED, out)
    return _GENERIC_TOKEN.sub(_redact_generic, out)


def _redact_userinfo_pair(value: str) -> str:
    """``user:pass`` -> ``user:<redacted>``; a bare value is redacted whole.

    A URL handed to a credential flag (``gh repo clone -u https://...``) is
    scrubbed as a URL instead, so the host that identifies the command is not
    lost to a naive split on its scheme separator.
    """
    stripped = value.strip(_QUOTES)
    if "://" in stripped:
        return redact_text(value)
    user, separator, _password = stripped.partition(":")
    if separator:
        return f"{user}:{REDACTED}"
    return REDACTED


def _redact_header(value: str) -> str:
    stripped = value.strip(_QUOTES)
    name, separator, _rest = stripped.partition(":")
    if separator and is_secret_name(name):
        return f"{name.strip()}: {REDACTED}"
    return redact_text(value)


def _tool_key(tool: str | None) -> str:
    """The bare binary name, without directory or Windows extension."""
    if not tool:
        return ""
    name = str(tool).replace("\\", "/").rsplit("/", 1)[-1].lower()
    for suffix in (".exe", ".cmd", ".bat", ".com"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _credential_flags(tool: str | None) -> frozenset[str]:
    """The flags that mean "the next argument is a credential" for ``tool``."""
    return _CREDENTIAL_FLAGS | _TOOL_CREDENTIAL_FLAGS.get(_tool_key(tool), frozenset())


def _subcommand_flags(tool: str | None) -> dict[str, frozenset[str]]:
    """The subcommand-scoped credential flags ``tool`` brings into scope."""
    return dict(_TOOL_SUBCOMMAND_CREDENTIAL_FLAGS.get(_tool_key(tool), {}))


def redact_argv(args: Iterable[str] | None, *, tool: str | None = None) -> list[str]:
    """Redact an argument vector, using each flag to interpret its value.

    Position matters, which is why this is not a plain ``map(redact_text)``:
    ``-u`` and ``-H`` say what their *next* argument means, and a value that
    would otherwise look ordinary (``alice:hunter2``, ``Authorization: Bearer
    x``) is only identifiable from the flag in front of it. ``tool`` scopes the
    overloaded short flags, so ``curl -u alice:pw`` is redacted while
    ``python -u script.py`` and ``ssh -p 2222`` are left readable - an operator
    cannot approve a command whose target has been redacted away.

    A wrapper does not lose that scoping: a positional argument that names a
    credential-flag tool widens the set from that point on, so ``sudo curl -u
    alice:pw`` and ``env FOO=1 curl -u alice:pw`` are redacted like a bare
    ``curl``. A positional that names a *subcommand* which makes a short flag
    a credential widens it the same way, so ``docker login -p hunter2`` is
    redacted while ``docker run -p 8080:80`` keeps its port.
    """
    if args is None:
        return []
    credential_flags = _credential_flags(tool)
    subcommand_flags = _subcommand_flags(tool)
    out: list[str] = []
    pending: str | None = None
    for raw in args:
        token = str(raw)
        if pending is not None:
            flag, pending = pending, None
            if flag in _HEADER_FLAGS:
                out.append(_redact_header(token))
            elif flag in _BODY_FLAGS or flag in _ASSIGNMENT_FLAGS:
                out.append(redact_text(token))
            elif flag in credential_flags:
                out.append(_redact_userinfo_pair(token))
            else:
                out.append(REDACTED)
            continue
        name, separator, value = token.partition("=")
        if separator and token.startswith("-"):
            if name in credential_flags:
                out.append(f"{name}={_redact_userinfo_pair(value)}")
            elif name in _HEADER_FLAGS:
                out.append(f"{name}={_redact_header(value)}")
            elif is_secret_name(name):
                out.append(f"{name}={REDACTED}")
            else:
                out.append(redact_text(token))
            continue
        if token.startswith("-") and (
            is_credential_flag_name(token)
            or token in credential_flags
            or token in _HEADER_FLAGS
            or token in _BODY_FLAGS
            or token in _ASSIGNMENT_FLAGS
        ):
            pending = token
            out.append(token)
            continue
        if len(token) > 2 and not token.startswith("--") and token[:2] in credential_flags:
            # ``mysql -pSECRET``, ``redis-cli -aSECRET``: the credential is
            # attached to its flag, so there is no next argument to redact.
            out.append(f"{token[:2]}{REDACTED}")
            continue
        if not token.startswith("-"):
            key = _tool_key(token)
            credential_flags |= _TOOL_CREDENTIAL_FLAGS.get(key, frozenset())
            credential_flags |= subcommand_flags.get(key, frozenset())
            subcommand_flags.update(_TOOL_SUBCOMMAND_CREDENTIAL_FLAGS.get(key, {}))
        out.append(redact_text(token))
    return out


def redact_command_text(text: str, *, tool: str | None = None) -> str:
    """Redact free text that *is* a command line, using ``tool``'s flag grammar.

    :func:`redact_text` cannot see position, and position is exactly what makes
    a short flag a credential: ``docker login -p hunter2`` and ``redis-cli -a
    hunter2`` carry nothing that looks secret on its own. A summary is a sink
    like every other - it reaches the stored row, the ASK an operator reads and
    the hash-chained entry - so the argument-vector rules are applied to its
    whitespace-separated tokens as well.

    The free-text pass runs *first* so shapes that span tokens (a quoted
    ``-H "Authorization: ****** header) are caught before splitting, and
    redaction is idempotent, so a summary already built from
    :func:`redact_argv` is unchanged apart from whitespace. Line structure is
    kept, because an error message that echoes a command line is one of the
    things this is used for and a one-line blob is harder to act on.
    """
    if not text:
        return ""
    return "\n".join(
        " ".join(redact_argv(line.split(), tool=tool)) for line in redact_text(text).splitlines()
    )


def redact_mapping(args: dict) -> list[str]:
    """``{name: value}`` -> a sorted, redacted ``name=value`` vector."""
    return [
        f"{key}={REDACTED if is_secret_name(str(key)) else redact_text(str(args[key]))}"
        for key in sorted(args, key=str)
    ]


def contains_secret(text: str) -> bool:
    """True when :func:`redact_text` would change ``text``."""
    return redact_text(text) != (text or "")


__all__ = [
    "REDACTED",
    "SECRET_NAME_PARTS",
    "SECRET_NAME_SEGMENTS",
    "contains_secret",
    "is_credential_flag_name",
    "is_secret_name",
    "redact_argv",
    "redact_command_text",
    "redact_mapping",
    "redact_text",
]
