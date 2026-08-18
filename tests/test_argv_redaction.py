"""Secret redaction: what a governed action is allowed to remember.

Every case here is a shape that reached the audit chain, the stored summary,
the ASK body or a bridge message before this module existed. The suite also
asserts the other half of the contract - that ordinary arguments survive
untouched - because an approval digest that redacts everything cannot tell one
command from another, and an operator approving a redacted blank is not
approving anything.
"""

from __future__ import annotations

import time
import uuid

import pytest

from brains.control.sessions import register_workspace
from brains.exec import gate
from brains.govern import ActionTarget, GovernedRequest, args_digest, normalize_args
from brains.govern.redaction import REDACTED, contains_secret, redact_argv, redact_text

_GH_TOKEN = "fake_token_" + "A1b2C3d4E5f6G7h8I9j0" * 2
_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r"
_AWS_KEY = "fake_token_ABCDEF1234567890ABCDEF1234567890"
_OPAQUE = "Zx8kQ2vL9pR4tY7wA1sD3fG6hJ0kL5mN"


# ----------------------------------------------------------------------
# URL credentials
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "leak"),
    [
        (["clone", f"https://user:{_GH_TOKEN}@github.com/o/r.git"], _GH_TOKEN),
        (["clone", "https://oauth2:hunter2@gitlab.com/o/r.git"], "hunter2"),
        (["-u", f"https://{_GH_TOKEN}@github.com/o/r.git"], _GH_TOKEN),
        (["--url", "postgres://brains:s3cr3tpw@db.internal:5432/brains"], "s3cr3tpw"),
        (["push", "https://x-access-token:ghs_AbCdEfGh12345678ijkl@github.com/o/r"], "ghs_"),
    ],
)
def test_url_credentials_never_survive(argv, leak):
    redacted = " ".join(redact_argv(argv))
    assert leak not in redacted
    assert REDACTED in redacted
    # The host is what identifies the command; it must survive.
    assert "github.com" in redacted or "gitlab.com" in redacted or "db.internal" in redacted


# ----------------------------------------------------------------------
# NAME=VALUE, with or without a flag prefix
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "leak"),
    [
        (["--token", "hunter2", "push"], "hunter2"),
        (["--api-key=abcdef123456"], "abcdef123456"),
        (["env", "GITHUB_TOKEN=" + _GH_TOKEN, "gh", "auth"], _GH_TOKEN),
        (["run", "-e", "API_KEY=abcdef123456", "image"], "abcdef123456"),
        (["run", "-e", "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG", "image"], "wJalrXUtnFEMI"),
        (["set", "BRAINS_ADMIN_PASSWORD=hunter2"], "hunter2"),
        (["--password", "hunter2"], "hunter2"),
        (["--client-secret=abcdef123456"], "abcdef123456"),
    ],
)
def test_named_secrets_are_redacted_whatever_the_prefix(argv, leak):
    redacted = " ".join(redact_argv(argv))
    assert leak not in redacted
    assert REDACTED in redacted


def test_windows_and_quoted_spellings_are_redacted_too():
    """PowerShell/cmd spellings must not be a way around the same rule."""
    cases = [
        (['GITHUB_TOKEN="' + _GH_TOKEN + '"'], _GH_TOKEN),
        (["set", "TOKEN=hunter2", "&&", "gh", "auth"], "hunter2"),
        (["--token='hunter2'"], "hunter2"),
        (["-Password", "hunter2"], "hunter2"),
        (["$env:GITHUB_TOKEN=" + _GH_TOKEN], _GH_TOKEN),
        (["C:\\Program Files\\gh\\gh.exe", "auth", "login", "--with-token", "hunter2"], "hunter2"),
    ]
    for argv, leak in cases:
        redacted = " ".join(redact_argv(argv))
        assert leak not in redacted, argv
        assert REDACTED in redacted, argv


def test_windows_paths_are_not_mistaken_for_secrets():
    argv = ["C:\\Users\\alice\\projects\\brains-v2\\run.py", "--out", "D:\\data\\report.json"]
    assert redact_argv(argv) == argv


# ----------------------------------------------------------------------
# Bare credential words, bounded by segment
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "tool", "leak"),
    [
        # env / NAME=VALUE
        (["run", "-e", "DB_PASS=hunter2", "image"], "docker", "hunter2"),
        (["run", "-e", "MASTER_KEY=hunter2", "image"], "docker", "hunter2"),
        (["env", "REDIS_PASS=hunter2", "psql"], None, "hunter2"),
        (["set", "SIGNING_KEYS=hunter2"], None, "hunter2"),
        (["deploy", "--env=APP_KEY=hunter2"], None, "hunter2"),
        # flag names
        (["--db-pass", "hunter2"], None, "hunter2"),
        (["--master-key=hunter2"], None, "hunter2"),
        (["-DbPass", "hunter2"], None, "hunter2"),
        # header names
        (["curl", "-H", "X-Signing-Key: hunter2", "https://api.example.com"], None, "hunter2"),
        # request bodies
        (["-d", "user=alice&db_pass=hunter2&scope=repo"], "curl", "hunter2"),
        (["--json", '{"user":"alice","apiKey":"hunter2xyz"}'], "curl", "hunter2xyz"),
    ],
)
def test_bare_pass_and_key_names_are_redacted(argv, tool, leak):
    redacted = " ".join(redact_argv(argv, tool=tool))
    assert leak not in redacted, argv
    assert REDACTED in redacted, argv


def test_bare_credential_words_do_not_overmatch_ordinary_names():
    """Over-redaction destroys the digest's ability to identify a command."""
    argv = [
        "run",
        "--bypass=cache",
        "--passenger-name=alice",
        "--keyboard-layout=us",
        "--monkey-patch=on",
        "-e",
        "PASSENGER_APP_ENV=staging",
        "-e",
        "KEYBOARD_LAYOUT=us",
        "-e",
        "BYPASS_CACHE=1",
        "-e",
        "MONKEYPATCH=1",
    ]
    assert redact_argv(argv, tool="docker") == argv
    assert not contains_secret(" ".join(argv))
    assert redact_text("bypass=cache passenger=alice keyboard=us monkey=1") == (
        "bypass=cache passenger=alice keyboard=us monkey=1"
    )


# ----------------------------------------------------------------------
# A lone bare word names a resource, not the argument after it
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "tool"),
    [
        # The object an operator is being asked to approve acting on.
        (["s3api", "get-object", "--bucket", "prod", "--key", "prod/db/backup.tar.gz"], "aws"),
        (["s3api", "delete-object", "--bucket", "prod", "--key", "2026/01/report.pdf"], "aws"),
        (["dynamodb", "delete-item", "--key", '{"id": "user-42"}'], "aws"),
        (["kv", "get", "--key", "feature-flags/checkout"], "wrangler"),
        (["--keys", "user:42:profile"], "redis-cli"),
        (["--cred", "deploy-bot"], "custom"),
    ],
)
def test_a_lone_bare_word_flag_keeps_the_resource_it_names(argv, tool):
    """``--key`` names an object far more often than it names a credential.

    Redacting its operand made every object in a bucket share one approval
    digest, and left the operator approving ``aws ... --key <redacted>``.
    """
    assert redact_argv(argv, tool=tool) == argv
    assert not contains_secret(" ".join(argv))


def test_different_resource_keys_are_different_actions():
    """The digest and the ASK summary must tell one object from another."""
    left = ["s3api", "delete-object", "--bucket", "prod", "--key", "db/backup-2026-01.tar.gz"]
    right = ["s3api", "delete-object", "--bucket", "prod", "--key", "db/backup-2026-02.tar.gz"]

    assert normalize_args(left, "aws") != normalize_args(right, "aws")
    assert args_digest("exec.command", "aws", left) != args_digest("exec.command", "aws", right)

    decision = gate.classify("aws", left)
    assert decision.gate is True
    assert "db/backup-2026-01.tar.gz" in decision.summary
    assert decision.summary != gate.classify("aws", right).summary


@pytest.mark.parametrize(
    ("argv", "tool", "leak"),
    [
        # ...but the same bare word still redacts a value bound to it,
        (["--key=hunter2secretvalue"], None, "hunter2secretvalue"),
        (["run", "-e", "KEY=hunter2secretvalue", "image"], "docker", "hunter2secretvalue"),
        (["curl", "-H", "Key: hunter2secretvalue", "https://api.example.com"], None, "hunter2"),
        (["-d", "user=alice&key=hunter2secretvalue"], "curl", "hunter2secretvalue"),
        (["--json", '{"user":"alice","key":"hunter2secretvalue"}'], "curl", "hunter2secretvalue"),
        # a value that is secret-shaped on its own,
        (["s3api", "put-object", "--key", _GH_TOKEN], "aws", _GH_TOKEN),
        (["--key", _OPAQUE], None, _OPAQUE),
        (["--cred", _AWS_KEY], None, _AWS_KEY),
        # and every qualified name keeps claiming the argument after it.
        (["--db-pass", "hunter2"], None, "hunter2"),
        (["--master-key", "hunter2"], None, "hunter2"),
        (["-DbPass", "hunter2"], None, "hunter2"),
        (["--signing-key", "hunter2"], None, "hunter2"),
        (["--api-key", "hunter2"], None, "hunter2"),
        (["--password", "hunter2"], None, "hunter2"),
        (["--pass", "hunter2"], None, "hunter2"),
        (["-u", "alice:hunter2"], "curl", "hunter2"),
    ],
)
def test_a_bare_word_still_redacts_bound_values_and_secret_shapes(argv, tool, leak):
    redacted = " ".join(redact_argv(argv, tool=tool))
    assert leak not in redacted, argv
    assert REDACTED in redacted, argv


# ----------------------------------------------------------------------
# curl and friends
# ----------------------------------------------------------------------


def test_curl_user_flag_keeps_the_identity_and_drops_the_password():
    assert redact_argv(["-u", "alice:hunter2", "https://api.example.com"], tool="curl") == [
        "-u",
        f"alice:{REDACTED}",
        "https://api.example.com",
    ]
    assert redact_argv(["--user=alice:hunter2"]) == [f"--user=alice:{REDACTED}"]
    assert "hunter2" not in " ".join(redact_argv(["--user", "alice:hunter2"]))
    assert "hunter2" not in " ".join(redact_argv(["-phunter2", "brains"], tool="mysql"))


@pytest.mark.parametrize(
    ("tool", "argv"),
    [
        ("python", ["-u", "deploy_prod.py"]),
        ("sort", ["-u", "/etc/hosts"]),
        ("ssh", ["-p", "2222", "prod.example.com"]),
        ("docker", ["run", "-p", "8080:22", "nginx"]),
        ("psql", ["-p", "5432", "-d", "brains"]),
        # ``-p`` only means a password under ``login``; ``docker ps`` and
        # ``podman run`` publish ports and list containers.
        ("docker", ["ps", "-a", "--format", "{{.Names}}"]),
        ("podman", ["run", "-p", "5432:5432", "postgres"]),
        # redis-cli's ``-p`` is the port - only ``-a`` is its password.
        ("redis-cli", ["-p", "6379", "ping"]),
        # ``wget -b`` is ``--background`` and takes no value at all, so the
        # URL after it is the target an operator is approving.
        ("wget", ["-b", "https://example.com/dataset.tar.gz"]),
        ("git", ["log", "-p", "-1"]),
    ],
)
def test_overloaded_short_flags_are_not_treated_as_credentials(tool, argv):
    """``-u``/``-p``/``-a``/``-b`` mean something different for almost every binary.

    Redacting them everywhere would hide the very thing an operator approves -
    the script, the file, the port - and would make two different commands
    share one approval digest.
    """
    assert redact_argv(argv, tool=tool) == argv
    left = args_digest("exec.command", tool, argv)
    right = args_digest("exec.command", tool, [*argv[:-1], "something-else"])
    assert left != right, "the digest cannot tell two different commands apart"


@pytest.mark.parametrize(
    ("tool", "argv", "leak", "kept"),
    [
        ("docker", ["login", "-u", "alice", "-p", "hunter2", "ghcr.io"], "hunter2", "ghcr.io"),
        ("docker", ["login", "-phunter2", "ghcr.io"], "hunter2", "ghcr.io"),
        ("podman", ["login", "-p", "hunter2", "quay.io"], "hunter2", "quay.io"),
        ("nerdctl", ["login", "-p", "hunter2", "ghcr.io"], "hunter2", "ghcr.io"),
        (
            "curl",
            ["-b", "SESSION=hunter2", "https://api.example.com/deploy"],
            "hunter2",
            "https://api.example.com/deploy",
        ),
        ("redis-cli", ["-a", "hunter2", "ping"], "hunter2", "ping"),
        ("redis-cli", ["-ahunter2", "ping"], "hunter2", "ping"),
        ("mongosh", ["-u", "alice", "-p", "hunter2", "mongodb://db/brains"], "hunter2", "alice"),
        ("sshpass", ["-p", "hunter2", "ssh", "prod.example.com"], "hunter2", "prod.example.com"),
        ("mariadb", ["-phunter2", "brains"], "hunter2", "brains"),
        ("mosquitto_pub", ["-P", "hunter2", "-t", "deploys"], "hunter2", "deploys"),
    ],
)
def test_short_credential_flags_are_redacted_for_the_tools_that_mean_them(tool, argv, leak, kept):
    """The short forms an audit of real command lines turned up.

    Each one is a credential *for this tool* (and, for ``docker login``, only
    under this subcommand), so the value goes and the target stays: an
    operator still sees which registry, host or endpoint they are approving.
    """
    redacted = redact_argv(argv, tool=tool)
    joined = " ".join(redacted)
    assert leak not in joined, redacted
    assert REDACTED in joined, redacted
    assert kept in joined, redacted


def test_short_credential_flags_survive_a_wrapper_and_the_gate_summary():
    """``sudo docker login -p`` is scoped from the positional, not the binary."""
    redacted = " ".join(redact_argv(["docker", "login", "-p", "hunter2", "ghcr.io"], tool="sudo"))
    assert "hunter2" not in redacted
    assert REDACTED in redacted
    decision = gate.classify("docker", ["login", "-u", "alice", "-p", "hunter2", "ghcr.io"])
    assert "hunter2" not in decision.summary
    assert REDACTED in decision.summary


def test_paths_and_uris_survive_redaction():
    """A destructive target must stay visible in the ASK and in the digest."""
    argv = ["s3", "rm", "s3://Bucket1/prod/Backups/latest/snapshot-2026.tar.gz"]
    assert redact_argv(argv, tool="aws") == argv
    assert redact_text("/home/alice/Projects/brainsV2/src/main.py") == (
        "/home/alice/Projects/brainsV2/src/main.py"
    )
    assert args_digest("exec.command", "aws", argv) != args_digest(
        "exec.command", "aws", ["s3", "rm", "s3://Bucket9/prod/Backups/latest/other.tar.gz"]
    )


def test_bare_token_flags_are_redacted_even_without_a_header_shape():
    """``--oauth2-bearer`` carries a naked token, not ``Name: Value``."""
    assert "deadbeefcafe1234" not in " ".join(
        redact_argv(["--oauth2-bearer", "deadbeefcafe1234"], tool="curl")
    )


def test_json_bodies_lose_their_credential_fields():
    body = '{"client_id":"brains","client_secret":"hunter2xyz","scope":"repo"}'
    redacted = redact_argv(["--json", body], tool="curl")[1]
    assert "hunter2xyz" not in redacted
    assert "brains" in redacted, "the non-secret fields identify the request"
    assert REDACTED in redacted


def test_nested_assignments_are_redacted_however_they_are_spelled():
    """An innocent assignment must not swallow the secret nested inside it."""
    cases = [
        "command failed: PGPASSWORD=hunter2 psql -h db",
        "command failed:PGPASSWORD=hunter2 psql",
        "env=PGPASSWORD=hunter2",
        "docker run --env=API_KEY=hunter2 img",
        "helm install x --set=redis.password=hunter2",
    ]
    for case in cases:
        assert "hunter2" not in redact_text(case), case
        assert REDACTED in redact_text(case), case
    assert "hunter2" not in " ".join(
        redact_argv(["run", "--env=API_KEY=hunter2", "img"], tool="docker")
    )
    assert "hunter2" not in " ".join(
        redact_argv(["install", "x", "--set=redis.password=hunter2"], tool="helm")
    )


def test_a_wrapper_does_not_defeat_tool_scoped_credential_flags():
    """``sudo``/``env``/``timeout`` in front of curl must not leak its password."""
    for wrapper in (["sudo"], ["env", "FOO=1"], ["timeout", "5"]):
        argv = [*wrapper, "curl", "-u", "alice:hunter2", "https://api.example.com"]
        redacted = " ".join(redact_argv(argv[1:], tool=argv[0]))
        assert "hunter2" not in redacted, argv
        assert REDACTED in redacted, argv


def test_redaction_of_a_long_opaque_argument_stays_cheap():
    """The gate redacts before it records, so a big body must not stall it."""
    blob = "a1B2" * 25_000  # 100k characters, no delimiters
    start = time.perf_counter()
    redact_argv(["-d", blob], tool="curl")
    elapsed = time.perf_counter() - start
    assert elapsed < 5.0, f"redaction of a 100k argument took {elapsed:.1f}s"


def test_free_text_header_values_lose_the_token_after_the_scheme():
    """A failure message that echoes its own command line is free text."""
    leaked = 'curl -H "Authorization: Bearer 1234567890abcdefABCDEF" https://api'
    redacted = redact_text(leaked)
    assert "1234567890abcdefABCDEF" not in redacted
    assert "https://api" in redacted
    assert "hunter2" not in redact_text("command failed: PGPASSWORD=hunter2 psql -h db")


@pytest.mark.parametrize(
    "header",
    [
        f"Authorization: Bearer {_JWT}",
        "Authorization: token hunter2secretvalue",
        "Cookie: session=abcdef123456; other=1",
        "X-Api-Key: abcdef123456",
        "Proxy-Authorization: Basic YWxpY2U6aHVudGVyMg==",
    ],
)
def test_header_values_that_carry_credentials_are_redacted(header):
    redacted = " ".join(redact_argv(["curl", "-H", header, "https://api.example.com"]))
    name = header.split(":", 1)[0]
    assert name in redacted, "the header name identifies the request and must survive"
    assert header.split(":", 1)[1].strip() not in redacted
    assert REDACTED in redacted


def test_ordinary_headers_are_left_alone():
    argv = ["curl", "-H", "Accept: application/json", "https://api.example.com/v1/items"]
    assert redact_argv(argv) == argv


def test_request_bodies_lose_only_their_credential_fields():
    redacted = redact_argv(
        ["curl", "-d", "user=alice&password=hunter2&scope=repo", "https://api.example.com"]
    )
    body = redacted[2]
    assert "user=alice" in body
    assert "scope=repo" in body
    assert "hunter2" not in body
    assert REDACTED in body


# ----------------------------------------------------------------------
# Token shapes
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "secret",
    [
        _GH_TOKEN,
        "fake-token-11ABCDEFG0aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789",
        "fake-token-ABCDEFGhijkl1234567890XYZXYZXYZ",
        "fake-token-123456789012-123456789012-AbCdEfGhIjKlMnOpQrStUvWx",
        "fake-token-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
        _AWS_KEY,
        "fake-token-A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r",
        _JWT,
    ],
)
def test_known_token_shapes_are_redacted_wherever_they_appear(secret):
    assert secret not in " ".join(redact_argv(["deploy", secret, "--to", "prod"]))
    assert secret not in redact_text(f"the command was: deploy {secret} --to prod")


def test_high_confidence_opaque_tokens_are_redacted():
    assert REDACTED in redact_argv(["login", _OPAQUE])[1]


def test_ordinary_arguments_are_not_over_redacted():
    """Over-redaction destroys the digest's ability to identify a command."""
    argv = [
        "push",
        "--force-with-lease",
        "origin",
        "refs/heads/fix/durable-audit-gate",
        "3f7a1c9e2b8d4a6f0c5e1d3b7a9f2c4e6d8b0a1c",  # a git SHA, not a token
        "--message=deploy release 2026.08.01",
        "https://github.com/owner/repo.git",
        "path/to/some_file.tar.gz",
    ]
    assert redact_argv(argv) == argv
    assert not contains_secret(" ".join(argv))


# ----------------------------------------------------------------------
# The digest and every sink
# ----------------------------------------------------------------------


def test_digest_binds_the_redacted_shape_not_the_secret():
    left = args_digest("exec.command", "curl", ["-H", f"Authorization: Bearer {_JWT}", "https://x"])
    right = args_digest("exec.command", "curl", ["-H", "Authorization: Bearer other", "https://x"])
    different = args_digest("exec.command", "curl", ["-H", "Authorization: Bearer x", "https://y"])

    assert left == right, "the digest depends on the secret"
    assert left != different, "the digest cannot tell two different commands apart"
    assert REDACTED in " ".join(normalize_args(["-H", f"Authorization: Bearer {_JWT}"]))


def test_summary_is_redacted_on_the_request_itself(tmp_path):
    """Every sink reads the summary from here, so redaction happens here."""
    workspace = tmp_path / "ws-redaction"
    workspace.mkdir()
    registered = register_workspace(str(workspace))
    request = GovernedRequest(
        actor="tester",
        action="exec.command",
        tool="git",
        args=["clone", f"https://user:{_GH_TOKEN}@github.com/o/r.git"],
        target=ActionTarget(workspace_id=registered.id, workspace_path=str(workspace)),
        summary=f"git clone https://user:{_GH_TOKEN}@github.com/o/r.git",
        idempotency_key=f"test:{uuid.uuid4().hex}",
    )

    assert _GH_TOKEN not in request.summary
    assert REDACTED in request.summary


def test_no_secret_reaches_the_chain_the_row_or_the_ask(tmp_path):
    from brains.audit import list_entries
    from brains.control.decisions import get_decision
    from brains.govern import authorize, get_governed_action

    workspace = tmp_path / "ws-redaction-sinks"
    workspace.mkdir()
    registered = register_workspace(str(workspace))
    request = GovernedRequest(
        actor="tester",
        action="exec.command",
        tool="curl",
        args=["-H", f"Authorization: Bearer {_JWT}", "https://api.example.com/deploy"],
        target=ActionTarget(workspace_id=registered.id, workspace_path=str(workspace)),
        tier="outward",
        summary=f"curl -H 'Authorization: Bearer {_JWT}' https://api.example.com/deploy",
        idempotency_key=f"test:{uuid.uuid4().hex}",
    )

    decision = authorize(request, wait=False, notify=False)

    row = get_governed_action(decision.action_id)
    ask = get_decision(decision.approval_code)
    entries = [
        entry
        for entry in list_entries(action_prefix="governed.", limit=50)
        if entry["payload"].get("action_id") == decision.action_id
    ]
    assert entries
    for blob in (str(row), str(ask), str(entries)):
        assert _JWT not in blob
        assert "eyJhbGciOiJIUzI1NiJ9" not in blob


def test_a_short_flag_password_reaches_no_sink(tmp_path):
    """``docker login -p`` end to end: row, ASK body and audit chain.

    The short forms are the ones that used to survive, because they are only
    credentials for some tools - so this asserts the whole path, not just the
    normaliser: the stored summary, the approval an operator reads, and the
    hash-chained entry that can never be rewritten.
    """
    from brains.audit import list_entries
    from brains.control.decisions import get_decision
    from brains.govern import authorize, get_governed_action

    workspace = tmp_path / "ws-redaction-short-flags"
    workspace.mkdir()
    registered = register_workspace(str(workspace))
    argv = ["login", "-u", "alice", "-p", "hunter2paSSword9", "ghcr.io"]
    request = GovernedRequest(
        actor="tester",
        action="exec.command",
        tool="docker",
        args=argv,
        target=ActionTarget(workspace_id=registered.id, workspace_path=str(workspace)),
        tier="outward",
        summary="docker login -u alice -p hunter2paSSword9 ghcr.io",
        idempotency_key=f"test:{uuid.uuid4().hex}",
    )

    decision = authorize(request, wait=False, notify=False)

    row = get_governed_action(decision.action_id)
    ask = get_decision(decision.approval_code)
    entries = [
        entry
        for entry in list_entries(action_prefix="governed.", limit=50)
        if entry["payload"].get("action_id") == decision.action_id
    ]
    assert entries
    for blob in (str(row), str(ask), str(entries)):
        assert "hunter2paSSword9" not in blob
    assert "ghcr.io" in str(row), "the registry identifies what is being approved"
    # The digest binds the redacted shape, so rotating the password does not
    # invalidate the approval - and neither does it reveal it.
    assert args_digest("exec.command", "docker", argv) == args_digest(
        "exec.command", "docker", ["login", "-u", "alice", "-p", "other-pw", "ghcr.io"]
    )


def test_a_failure_message_that_echoes_the_command_is_redacted(tmp_path):
    from brains.audit import list_entries
    from brains.govern import TIER_LOCAL, complete, get_governed_action, reserve

    workspace = tmp_path / "ws-redaction-error"
    workspace.mkdir()
    registered = register_workspace(str(workspace))
    request = GovernedRequest(
        actor="tester",
        action="exec.command",
        tool="gh",
        args=["auth", "login"],
        target=ActionTarget(workspace_id=registered.id, workspace_path=str(workspace)),
        tier=TIER_LOCAL,
        summary="gh auth login",
        idempotency_key=f"test:{uuid.uuid4().hex}",
    )
    snapshot, _replayed = reserve(request)

    complete(
        request,
        snapshot["action_id"],
        ok=False,
        error=f"command failed: gh auth login --with-token {_GH_TOKEN}",
    )

    row = get_governed_action(snapshot["action_id"])
    entries = [
        entry
        for entry in list_entries(action_prefix="governed.failed", limit=50)
        if entry["payload"].get("action_id") == snapshot["action_id"]
    ]
    assert _GH_TOKEN not in str(row)
    assert _GH_TOKEN not in str(entries)
    assert REDACTED in (row["error"] or "")
