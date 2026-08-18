"""The canonical realtime topic grammar (BL-P0-02).

A realtime topic is not an arbitrary string a client invents. It is a name in a
closed grammar, and every name in that grammar maps to exactly one *scope
question* the server can answer from its own state:

===============================  ==========  =========================  ====================
Canonical topic                  entity      scope derived from         audience
===============================  ==========  =========================  ====================
``org/{org_id}/{channel}``       ``org``     the Org row                operator
``issue/{issue_code}``           ``issue``   the Issue's Project Org    operator
``session/{session_id}/{stream}````session``  the Session's Workspace    operator, own Runtime
``machine/{machine_id}/{stream}````runtime``  the machine's Runtime Org  operator, own Runtime
``runtime/{runtime_id}/{stream}````runtime``  the Runtime's Org          operator, own Runtime
===============================  ==========  =========================  ====================

This module is deliberately **pure**: it parses and canonicalises syntax and
says which entity, capability and audience a topic implies. It never touches
the database and never decides authorization. Resolution of the parsed
reference to an Org/Workspace, and the deny-by-default decision itself, live in
:mod:`brains.authz.policy` where every other scope question is answered.

Two syntactic rules matter for security and are asserted here rather than at
the transports:

* **No wildcards.** ``*``, ``#``, ``?``, ``%`` and ``..`` are not part of the
  grammar, so a client cannot ask for "everything" or walk out of its scope.
* **No unknown family or channel.** A topic whose family or channel is not in
  the closed vocabulary below is refused rather than guessed, which is what
  makes "the publisher and the subscriber agree" a property of the grammar
  instead of a convention.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Entity kinds a topic can name.
ENTITY_ORG = "org"
ENTITY_ISSUE = "issue"
ENTITY_SESSION = "agent_session"
ENTITY_RUNTIME = "runtime"

#: Audiences: which principal kind a topic family can ever be granted to.
AUDIENCE_OPERATOR = "operator"
AUDIENCE_RUNTIME = "runtime"

#: The Org-scoped channels the product publishes on. Adding a channel here is
#: the only way to make one subscribable, which keeps the grammar closed.
ORG_CHANNELS: frozenset[str] = frozenset(
    {
        "issues",
        "sessions",
        "runtimes",
        "inbox",
        "projects",
        "personas",
        "pods",
        "automation",
    }
)

#: Per-Session streams. ``stdout`` is the live transcript, ``chat`` the
#: operator/agent message channel, ``state`` the lifecycle transitions.
SESSION_STREAMS: frozenset[str] = frozenset({"stdout", "chat", "state"})

#: Per-machine streams a Runtime credential works from.
MACHINE_STREAMS: frozenset[str] = frozenset({"assignments", "control"})

#: Per-Runtime streams.
RUNTIME_STREAMS: frozenset[str] = frozenset({"assignments", "status"})

#: The alias a caller may use for "the install's default Org".
DEFAULT_ORG_ALIAS = "default"

#: One topic segment. Deliberately narrow: no wildcard, no separator, no
#: whitespace, no control character, no percent-escape.
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")

#: A whole topic is bounded so a subscription cannot be used to store data.
MAX_TOPIC_LENGTH = 160
MAX_SEGMENTS = 3

#: How many topics one subscribe message may name, and how many a connection
#: may hold. Each resolution is a database read, so an unbounded list is a
#: cheap way to make an authenticated socket expensive.
MAX_TOPICS_PER_REQUEST = 64
MAX_TOPICS_PER_CONNECTION = 256


@dataclass(frozen=True)
class ParsedTopic:
    """The syntax of one topic, before any database lookup.

    ``reference`` is the entity reference the family names (an Org id or slug,
    an Issue code, a Session id, a machine id, a Runtime id) and ``channel`` is
    the stream within it. ``canonical`` is filled in by the resolver once the
    reference has been resolved to a stable id.
    """

    family: str
    reference: str
    channel: str
    entity: str
    audiences: frozenset[str]

    @property
    def is_operator_topic(self) -> bool:
        return AUDIENCE_OPERATOR in self.audiences

    @property
    def is_runtime_topic(self) -> bool:
        return AUDIENCE_RUNTIME in self.audiences


def _valid_segments(topic: str) -> list[str] | None:
    if not topic or len(topic) > MAX_TOPIC_LENGTH:
        return None
    parts = topic.split("/")
    if not 2 <= len(parts) <= MAX_SEGMENTS:
        return None
    if any(_SEGMENT_RE.match(part) is None for part in parts):
        return None
    return parts


def parse_topic(topic: object) -> ParsedTopic | None:
    """Parse ``topic`` into the grammar, or ``None`` when it is not in it.

    ``None`` covers every refusal - a non-string, a malformed segment, a
    wildcard, an unknown family, an unknown channel - because the caller must
    answer all of them identically. Distinguishing "malformed" from "not yours"
    is exactly the disclosure this grammar exists to prevent.
    """
    if not isinstance(topic, str):
        return None
    parts = _valid_segments(topic)
    if parts is None:
        return None
    family = parts[0]

    if family == "org" and len(parts) == 3 and parts[2] in ORG_CHANNELS:
        return ParsedTopic(
            family="org",
            reference=parts[1],
            channel=parts[2],
            entity=ENTITY_ORG,
            audiences=frozenset({AUDIENCE_OPERATOR}),
        )
    if family == "issue" and len(parts) == 2:
        return ParsedTopic(
            family="issue",
            reference=parts[1],
            channel="events",
            entity=ENTITY_ISSUE,
            audiences=frozenset({AUDIENCE_OPERATOR}),
        )
    if family == "session" and len(parts) == 3 and parts[2] in SESSION_STREAMS:
        return ParsedTopic(
            family="session",
            reference=parts[1],
            channel=parts[2],
            entity=ENTITY_SESSION,
            audiences=frozenset({AUDIENCE_OPERATOR, AUDIENCE_RUNTIME}),
        )
    if family == "machine" and len(parts) == 3 and parts[2] in MACHINE_STREAMS:
        return ParsedTopic(
            family="machine",
            reference=parts[1],
            channel=parts[2],
            entity=ENTITY_RUNTIME,
            audiences=frozenset({AUDIENCE_OPERATOR, AUDIENCE_RUNTIME}),
        )
    if family == "runtime" and len(parts) == 3 and parts[2] in RUNTIME_STREAMS:
        return ParsedTopic(
            family="runtime",
            reference=parts[1],
            channel=parts[2],
            entity=ENTITY_RUNTIME,
            audiences=frozenset({AUDIENCE_OPERATOR, AUDIENCE_RUNTIME}),
        )
    return None


def valid_reference(value: object) -> bool:
    """True when ``value`` can appear as an entity reference in a topic.

    Publishers build topics from data (a Session id, a machine id). A value
    that is not a legal segment would mint a name no subscriber is allowed to
    ask for, which is a silent one-way stream rather than an error, so the
    builders below refuse it instead.
    """
    return isinstance(value, str | int) and _SEGMENT_RE.match(str(value)) is not None


def org_topic(org_id: object, channel: str) -> str:
    """The canonical Org topic for ``channel``.

    Raises :class:`ValueError` for a channel outside the grammar, so a
    publisher cannot invent a topic no subscriber is allowed to name.
    """
    if channel not in ORG_CHANNELS:
        raise ValueError(f"unknown Org channel: {channel!r}")
    if not valid_reference(org_id):
        raise ValueError(f"unnameable Org reference: {org_id!r}")
    return f"org/{org_id}/{channel}"


def session_topic(session_id: object, stream: str) -> str:
    if stream not in SESSION_STREAMS:
        raise ValueError(f"unknown Session stream: {stream!r}")
    if not valid_reference(session_id):
        raise ValueError(f"unnameable Session reference: {session_id!r}")
    return f"session/{session_id}/{stream}"


def issue_topic(code: object) -> str:
    if not valid_reference(code):
        raise ValueError(f"unnameable Issue reference: {code!r}")
    return f"issue/{code}"


def machine_topic(machine_id: object, stream: str) -> str:
    if stream not in MACHINE_STREAMS:
        raise ValueError(f"unknown machine stream: {stream!r}")
    if not valid_reference(machine_id):
        raise ValueError(f"unnameable machine reference: {machine_id!r}")
    return f"machine/{machine_id}/{stream}"


def runtime_topic(runtime_id: object, stream: str) -> str:
    if stream not in RUNTIME_STREAMS:
        raise ValueError(f"unknown Runtime stream: {stream!r}")
    if not valid_reference(runtime_id):
        raise ValueError(f"unnameable Runtime reference: {runtime_id!r}")
    return f"runtime/{runtime_id}/{stream}"


__all__ = [
    "AUDIENCE_OPERATOR",
    "AUDIENCE_RUNTIME",
    "DEFAULT_ORG_ALIAS",
    "ENTITY_ISSUE",
    "ENTITY_ORG",
    "ENTITY_RUNTIME",
    "ENTITY_SESSION",
    "MACHINE_STREAMS",
    "MAX_TOPICS_PER_CONNECTION",
    "MAX_TOPICS_PER_REQUEST",
    "MAX_TOPIC_LENGTH",
    "ORG_CHANNELS",
    "RUNTIME_STREAMS",
    "SESSION_STREAMS",
    "ParsedTopic",
    "issue_topic",
    "machine_topic",
    "org_topic",
    "parse_topic",
    "runtime_topic",
    "session_topic",
    "valid_reference",
]
