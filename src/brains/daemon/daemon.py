"""The daemon loop (WS1 §2–§6).

`register → heartbeat ∥ poll-claim-execute ∥ stream ∥ local-GC`. Spawns flow
through :func:`brains.exec.runner.run_session` so the gate stays intact; outward
actions the spawned agent attempts are gated by the installed shims exactly as in
any other brains session. The daemon adds no gating of its own.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
from typing import Any, cast

from brains.daemon.client import HubClient
from brains.daemon.config import DaemonConfig
from brains.daemon.detect import detect_tools, register_local_tools

_LOG = logging.getLogger("brains.daemon")


def _as_runtime_id(value: object) -> int | None:
    """A Runtime id as the int the handle registry keys on, or ``None``."""
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None


class Daemon:
    def __init__(self, config: DaemonConfig, *, client: HubClient | None = None) -> None:
        self.config = config
        self.client = client or HubClient(
            config.hub_url, config.operator_key, verify_tls=config.verify_tls
        )
        self._stop = threading.Event()
        self._active = 0
        self._active_lock = threading.Lock()
        # Hub-authoritative timing knobs (filled in after register).
        self.heartbeat_interval_s = config.heartbeat_interval_s
        self.assignments_poll_s = config.assignments_poll_s
        # Session commands are polled on their own cadence, and deliberately a
        # fast one: an operator pressing stop is waiting for it, and the poll
        # is one cheap read per Runtime.
        self.command_poll_s = max(1, min(config.assignments_poll_s, 3))

    # --- registration ----------------------------------------------------- #

    def build_register_payload(self, detected: list[dict]) -> dict:
        from brains.daemon.detect import _normalize_os

        return {
            "machine_id": self.config.machine_id,
            "machine_label": self.config.machine_label,
            "os": _normalize_os(),
            "daemon_version": _daemon_version(),
            "working_root": self.config.working_root,
            "org_id": self.config.org_id,
            "tools": [
                {
                    "tool": d["tool"],
                    "display_name": d["display_name"],
                    "capabilities": d["capabilities"],
                }
                for d in detected
            ],
        }

    def detect_and_register(self) -> dict:
        """Detect CLIs, upsert ``registered_tools`` locally, then register all
        runtimes on the hub. Adopts the hub's authoritative interval knobs."""
        detected = register_local_tools(self.config)
        payload = self.build_register_payload(detected)
        resp = self.client.register(payload)
        if resp.get("heartbeat_interval_s"):
            self.heartbeat_interval_s = int(resp["heartbeat_interval_s"])
        if resp.get("assignments_poll_s"):
            self.assignments_poll_s = int(resp["assignments_poll_s"])
            self.command_poll_s = max(1, min(self.assignments_poll_s, 3))
        return resp

    def enrol_once(self, token: str) -> dict:
        """Redeem an enrolment token to connect this machine WITHOUT an operator
        key (Connect a machine, F1). Detects the local CLIs and registers one
        runtime per CLI via the unauthenticated redeem route — the token is the
        credential. Returns the hub's ``{machine_id, runtimes, daemon_key,
        credential_id, org_id, expires_at}`` response.

        The hub mints a **Runtime-narrow, Org-bound** ``daemon_key``: it
        authorizes only this machine's Runtime operations (register, heartbeat,
        status, claim, execute) and no operator or admin API. We persist it to
        the daemon config so the ongoing loop authenticates WITHOUT the operator
        ever pasting an admin key, and the hub can revoke it on its own
        (``brains-ai credentials revoke``).
        """
        detected = detect_tools(self.config)
        clis = [{"tool": d["tool"], "version": d.get("version")} for d in detected]
        resp = self.client.redeem_enrolment(
            token, self.config.machine_id, clis, org_id=self.config.org_id
        )
        daemon_key = resp.get("daemon_key")
        if daemon_key:
            _persist_daemon_key(daemon_key)
            self.config.operator_key = daemon_key
        return resp

    def my_runtimes(self) -> list[dict]:
        return self.client.list_runtimes(machine_id=self.config.machine_id)

    # --- heartbeat -------------------------------------------------------- #

    def heartbeat_once(self, status: str = "online") -> dict:
        runtimes = self.my_runtimes()
        items = [
            {
                "id": rt["id"],
                "status": status,
                "health": "healthy",
                "load": {
                    "active_sessions": self._active,
                    "max_concurrent": self.config.max_concurrent,
                },
            }
            for rt in runtimes
        ]
        if not items:
            return {"runtimes": []}
        return self.client.heartbeat_batch(self.config.machine_id, items)

    # --- poll → claim → execute ------------------------------------------ #

    def poll_and_execute(self) -> list[dict]:
        """One poll cycle across this machine's runtimes. Returns a record per
        assignment acted on. The number of spawns in a single cycle is bounded by
        ``max_concurrent`` (in the threaded loop the active-session gauge enforces
        the same cap across overlapping cycles).

        A Runtime row with no Org is skipped. It is an unclaimed pre-Org
        registration: no Org owns it, so no Org's work can be scoped to it, and
        claiming an assignment through it would run somebody's work on a
        Runtime that answers to nobody. Re-registering the machine claims it and
        it starts polling again.
        """
        acted: list[dict] = []
        for rt in self.my_runtimes():
            if rt.get("status") != "online":
                continue
            if rt.get("org_id") is None:
                continue
            if self._cap_reached(len(acted)):
                break
            for assignment in self.client.get_assignments(rt["id"]):
                if self._cap_reached(len(acted)):
                    break
                claim = self.client.claim(rt["id"], assignment["assignment_id"])
                if not claim.get("claimed"):
                    continue
                acted.append(self._execute(rt["id"], assignment, claim))
        return acted

    def poll_help_reviews(self) -> list[dict]:
        """Claim and run bounded read-only reviews for this machine's tools."""
        from brains.control.help_execution import run_read_only_review

        acted: list[dict] = []
        for runtime in self.my_runtimes():
            if runtime.get("status") != "online" or runtime.get("health") != "healthy":
                continue
            if self._cap_reached(len(acted)):
                break
            try:
                reviews = self.client.get_help_reviews(runtime["id"])
            except Exception as exc:
                acted.append(self._diagnostic(runtime, "help_review_poll_failed", exc))
                continue
            for queued in reviews:
                if self._cap_reached(len(acted)):
                    break
                try:
                    claimed = self.client.claim_help_review(runtime["id"], queued["code"])
                    review = claimed.get("review") if claimed.get("claimed") else None
                    if review is None:
                        continue
                    result = run_read_only_review(
                        review,
                        source_path=str(review["workspace_path"]),
                        workspace_id=int(review["workspace_id"]),
                        session_id=str(review["session_id"]),
                        runtime_id=int(runtime["id"]),
                    )
                    completed = self.client.complete_help_review(
                        runtime["id"],
                        queued["code"],
                        session_id=review["session_id"],
                        answer=result.answer,
                        evidence=result.evidence,
                        returncode=result.returncode,
                        source_unchanged=result.source_unchanged,
                        error_code=result.error_code,
                    )
                    acted.append({"code": queued["code"], **completed})
                except Exception as exc:
                    acted.append(self._diagnostic(runtime, "help_review_failed", exc))
        return acted

    def _cap_reached(self, spawned_this_cycle: int) -> bool:
        with self._active_lock:
            return (self._active + spawned_this_cycle) >= self.config.max_concurrent

    def _execute(self, runtime_id, assignment: dict, claim: dict) -> dict:
        """Spawn a gated session for one claimed assignment, streaming lifecycle
        events back and acking the terminal state. Workspace anti-collision is via
        ``control.claims`` (self-expiring) in a ``finally``."""
        from brains.exec import runner

        with self._active_lock:
            self._active += 1
        # ``load_config`` always resolves ``working_root`` to a real directory.
        workspace_path = cast("str", assignment.get("workspace_path") or self.config.working_root)
        tool = assignment["tool"]
        model = assignment.get("model") or self._tool_model(tool)
        prompt = assignment.get("prompt", "")
        session_token = claim.get("session_token", "")
        claimed_ws = False
        session_id = None
        record: dict = {
            "assignment_id": assignment["assignment_id"],
            "issue_id": assignment.get("issue_id"),
            "runtime_id": runtime_id,
        }
        try:
            # Open the hub-side session row (stamps runtime/persona/issue FKs).
            session = self.client.open_session(
                runtime_id,
                persona_id=claim.get("persona_id"),
                issue_id=claim.get("issue_id"),
                workspace_path=workspace_path,
                tool=tool,
            )
            session_id = session.get("session_id")
            record["session_id"] = session_id

            # Anti-collision claim on the workspace (reused moat; never fatal).
            claimed_ws = self._claim_workspace(workspace_path, session_token, assignment)

            self._emit(runtime_id, session_id, 0, "lifecycle", {"state": "running"})
            self.client.ack(
                runtime_id, assignment["assignment_id"], "started", session_id=session_id
            )

            argv, feed = runner._build_tool_argv(tool, prompt, model)
            result = runner.run_session(
                argv,
                workspace_path,
                prompt=feed,
                tool=tool,
                operator=None,
                orient_query=assignment.get("orient_query"),
                # Run *as* the hub's Session rather than opening a second,
                # local one the hub has never heard of: one process, one
                # Session row, one terminal state (BL-P0-05).
                session_id=session_id,
                # ...and record which Runtime holds it, so reconciliation
                # reports each Runtime on this box only its own Sessions.
                runtime_id=runtime_id,
            )
            rc = int(result.get("returncode", 0)) if isinstance(result, dict) else 0
            record["returncode"] = rc

            self._emit(
                runtime_id,
                session_id,
                1,
                "lifecycle",
                {"state": "done" if rc == 0 else "failed", "returncode": rc},
            )
            state = "finished" if rc == 0 else "aborted"
            self._report_session_state(
                session_id,
                "completed" if rc == 0 else "failed",
                summary=f"agent session ({tool}) exited with rc={rc}",
            )
            self.client.ack(
                runtime_id,
                assignment["assignment_id"],
                state,
                session_id=session_id,
                returncode=rc,
            )
            record["state"] = state
        except Exception as exc:  # pragma: no cover - defensive
            record["error"] = str(exc)
            # The hub must not be left showing a Session as running because
            # this Runtime failed: a launch that never produced a terminal
            # state is reported as failed rather than left to diverge.
            self._report_session_state(
                session_id, "failed", summary=f"the Runtime could not complete the session: {exc}"
            )
            with contextlib.suppress(Exception):
                self.client.ack(runtime_id, assignment["assignment_id"], "aborted")
        finally:
            if claimed_ws:
                self._release_workspace(workspace_path, session_token)
            with self._active_lock:
                self._active -= 1
        return record

    def _report_session_state(
        self, session_id: str | None, state: str, *, summary: str | None = None
    ) -> None:
        """Tell the hub what became of a Session this Runtime was running.

        Best effort against the network, never against the truth: a state this
        Runtime cannot report is reconciled on the next startup instead of
        being silently assumed.
        """
        if not session_id:
            return
        with contextlib.suppress(Exception):
            self.client.set_session_state(session_id, state, summary=summary)

    # --- session commands (message / stop), BL-P0-05 ---------------------- #

    def poll_session_commands(self) -> list[dict]:
        """One claim-execute-acknowledge cycle over queued Session commands.

        The daemon is the only party that holds the agent process handles for
        the Sessions bound to *its* Runtime, so it is the only party that can
        deliver their messages and stops. Each command is claimed with a lease
        (exactly one consumer wins), executed against the process this Runtime
        actually owns, and acknowledged with the outcome that was *observed* -
        including the refusals, which is what keeps an operator from being told
        a message was delivered to a process that cannot receive one.

        A command that turns out to belong to another consumer - a second
        worker on this box, a Session started from the CLI, one re-bound while
        it was in flight - is handed back rather than settled. Sharing a
        machine is not owning a process, and answering ``not_owned`` on the
        owner's behalf would burn the operator's command before its owner ever
        saw it.
        """
        from brains.exec import session_dispatch

        acted: list[dict] = []
        for rt in self.my_runtimes():
            if rt.get("org_id") is None:
                continue
            try:
                commands = self.client.get_session_commands(rt["id"])
            except Exception as exc:
                acted.append(self._diagnostic(rt, "poll_failed", exc))
                continue
            for command in commands:
                if not session_dispatch.owns(command, runtime_id=rt.get("id")):
                    continue
                try:
                    claim = self.client.claim_session_command(rt["id"], command["command_id"])
                except Exception as exc:
                    acted.append(
                        self._diagnostic(rt, "claim_failed", exc, command_id=command["command_id"])
                    )
                    continue
                if not claim.get("claimed"):
                    continue
                claimed = claim.get("command") or command
                if not session_dispatch.owns(claimed, runtime_id=rt.get("id")):
                    try:
                        self.client.release_session_command(
                            rt["id"],
                            claimed["command_id"],
                            reason="this Runtime does not own the Session; requeued for its owner",
                        )
                    except Exception as exc:
                        acted.append(
                            self._diagnostic(
                                rt, "release_failed", exc, command_id=claimed["command_id"]
                            )
                        )
                        continue
                    acted.append(
                        {
                            "command_id": claimed["command_id"],
                            "kind": claimed.get("kind"),
                            "session_id": claimed.get("session_id"),
                            "result": "released",
                            "ok": False,
                        }
                    )
                    continue
                outcome = session_dispatch.execute(claimed)
                try:
                    self.client.ack_session_command(
                        rt["id"],
                        claimed["command_id"],
                        result=outcome.result,
                        ok=outcome.ok,
                        error=outcome.error,
                    )
                except Exception as exc:
                    acted.append(
                        self._diagnostic(
                            rt, "acknowledge_failed", exc, command_id=claimed["command_id"]
                        )
                    )
                acted.append(
                    {
                        "command_id": claimed["command_id"],
                        "kind": claimed.get("kind"),
                        "session_id": claimed.get("session_id"),
                        "result": outcome.result,
                        "ok": outcome.ok,
                    }
                )
        return acted

    def _diagnostic(
        self, runtime: dict, stage: str, exc: Exception, *, command_id: str | None = None
    ) -> dict:
        """Record - and log - one step that failed, instead of dropping it.

        A swallowed exception here is indistinguishable from "there was
        nothing to do", which is the difference between a Runtime that has
        reconciled and one that has silently stopped trying. The record is
        returned to the caller (``--once``, the tests) and logged for the
        loop, in the same shape for every stage so a reader can grep one word.
        """
        record = {
            "runtime_id": runtime.get("id"),
            "machine_id": runtime.get("machine_id"),
            "stage": stage,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
        if command_id is not None:
            record["command_id"] = command_id
        _LOG.warning(
            "session control %s for runtime %s: %s", stage, runtime.get("id"), record["error"]
        )
        return record

    def reconcile_sessions(self) -> list[dict]:
        """Reconcile the hub's view of this machine with what this process owns.

        Called on startup and after re-registration. A restarted daemon owns no
        process handles, so every Session the hub still shows running here is
        settled with a truthful summary instead of staying live forever, and
        its queued commands are cancelled rather than waiting for a consumer
        that no longer exists.

        Reconciliation is per Runtime, and each Runtime is told *only* the
        Sessions this process holds for it. One box commonly runs several
        Runtimes (one per CLI); reporting the machine's whole set to each of
        them made every sibling claim ownership of the others' Sessions, which
        the hub refuses as a foreign claim - so the sibling's own call failed
        and the stale rows it should have ended stayed running forever. A
        Runtime holding nothing now sends an empty list, which is a fact, and
        reconciles on it.
        """
        from brains.exec import session_channel

        grouped = session_channel.owned_session_ids_by_runtime()
        out: list[dict] = []
        for rt in self.my_runtimes():
            if rt.get("org_id") is None:
                continue
            owned = grouped.get(_as_runtime_id(rt.get("id")), [])
            try:
                result = self.client.reconcile_sessions(rt["id"], owned)
            except Exception as exc:
                out.append(self._diagnostic(rt, "reconcile_failed", exc))
                continue
            out.append({"runtime_id": rt["id"], **result})
        return out

    def _emit(self, runtime_id, session_id, seq: int, stream: str, payload: dict) -> None:
        if not session_id:
            return
        with contextlib.suppress(Exception):
            self.client.post_event(
                runtime_id,
                session_id,
                seq=seq,
                stream=stream,
                chunk=json.dumps(payload),
            )

    def _claim_workspace(self, workspace_path: str, token: str, assignment: dict) -> bool:
        if not workspace_path or not token:
            return False
        try:
            from brains.control.claims import claim_workspace

            claim_workspace(
                workspace_path,
                token,
                scope="exec",
                duration_minutes=120,
                metadata={
                    "assignment_id": assignment.get("assignment_id"),
                    "runtime": str(assignment.get("tool")),
                },
            )
            return True
        except Exception:
            return False

    def _release_workspace(self, workspace_path: str, token: str) -> None:
        try:
            from brains.control.claims import release_workspace

            release_workspace(workspace_path, token)
        except Exception:
            pass

    def _tool_model(self, tool: str) -> str | None:
        return self.config.tool_override(tool).model

    # --- orchestration ---------------------------------------------------- #

    def run_once(self, *, detect: bool = True) -> dict:
        """A single detect→register→heartbeat→poll cycle (used by ``--once`` and
        tests)."""
        register = self.detect_and_register() if detect else {}
        heartbeat = self.heartbeat_once()
        reconciled = self.reconcile_sessions()
        commands = self.poll_session_commands()
        reviews = self.poll_help_reviews()
        acted = self.poll_and_execute()
        return {
            "register": register,
            "heartbeat": heartbeat,
            "reconciled": reconciled,
            "commands": commands,
            "reviews": reviews,
            "acted": acted,
        }

    def run(self, *, once: bool = False) -> None:
        """Run the daemon. ``once`` does a single cycle; otherwise loop with a
        background heartbeat thread until :meth:`stop`.

        Reconciliation runs before any work is claimed: a restarted daemon owns
        no process handles, so the hub's view of what is running here has to be
        corrected before this process starts adding to it.

        Session commands are polled on their **own** thread. An assignment
        executes inline and blocks for the whole life of the agent CLI, so a
        command polled from the same loop could only ever be delivered while
        this Runtime owned nothing - which is exactly when a stop cannot be
        delivered. The command thread runs while the agent runs, which is when
        the handle a stop needs actually exists.
        """
        self.detect_and_register()
        with contextlib.suppress(Exception):
            self.reconcile_sessions()
        if once:
            with contextlib.suppress(Exception):
                self.poll_session_commands()
            with contextlib.suppress(Exception):
                self.poll_help_reviews()
            self.poll_and_execute()
            return
        hb = threading.Thread(target=self._heartbeat_loop, name="brains-daemon-hb", daemon=True)
        hb.start()
        commands = threading.Thread(
            target=self._command_loop, name="brains-daemon-commands", daemon=True
        )
        commands.start()
        stop_requests = threading.Thread(
            target=self._stop_request_loop,
            name="brains-daemon-stop-requests",
            daemon=True,
        )
        stop_requests.start()
        last_detect = time.monotonic()
        try:
            while not self._stop.is_set():
                with contextlib.suppress(Exception):
                    self.poll_help_reviews()
                with contextlib.suppress(Exception):
                    self.poll_and_execute()
                if time.monotonic() - last_detect > self.config.detect_interval_s:
                    with contextlib.suppress(Exception):
                        self.detect_and_register()
                    with contextlib.suppress(Exception):
                        self.reconcile_sessions()
                    last_detect = time.monotonic()
                self._stop.wait(self.assignments_poll_s)
        finally:
            self._shutdown()

    def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            with contextlib.suppress(Exception):
                self.heartbeat_once()
            self._stop.wait(self.heartbeat_interval_s)

    def _command_loop(self) -> None:
        """Drain the Session command queue independently of assignment work."""
        while not self._stop.is_set():
            with contextlib.suppress(Exception):
                self.poll_session_commands()
            self._stop.wait(self.command_poll_s)

    def _stop_request_loop(self) -> None:
        while not self._stop.is_set():
            if _consume_stop_request():
                self.stop()
                return
            self._stop.wait(0.5)

    def drain(self) -> list[dict]:
        """Set all local runtimes → draining (graceful; stop claiming new work)."""
        out = []
        for rt in self.my_runtimes():
            with contextlib.suppress(Exception):
                out.append(self.client.heartbeat(rt["id"], status="draining", health="healthy"))
        return out

    def stop(self) -> None:
        self._stop.set()

    def _shutdown(self) -> None:
        """Graceful shutdown: deregister local runtimes → offline."""
        for rt in self.my_runtimes():
            with contextlib.suppress(Exception):
                self.client.deregister(rt["id"])

    def status(self) -> dict:
        reachable = True
        runtimes: list[dict] = []
        try:
            runtimes = self.my_runtimes()
        except Exception:
            reachable = False
        return {
            "machine_id": self.config.machine_id,
            "machine_label": self.config.machine_label,
            "hub_url": self.config.hub_url,
            "hub_reachable": reachable,
            "detected": [
                {"tool": d["tool"], "version": d["version"], "binary": d["binary"]}
                for d in detect_tools(self.config)
            ],
            "runtimes": runtimes,
        }


def _daemon_version() -> str:
    try:
        from importlib.metadata import version

        return version("brains")
    except Exception:
        return "0.0.0"


def _persist_daemon_key(daemon_key: str) -> None:
    """Persist the enrolment-minted daemon key into the daemon config file so the
    ongoing loop (heartbeat/poll) authenticates without an admin key (ASK-0008).

    Merges into the existing ``daemon.json`` ``hub.operator_key`` rather than
    overwriting, and never raises — a persistence failure must not fail connect.
    """
    import json as _json

    from brains.daemon.config import _config_path

    try:
        path = _config_path()
        raw: dict = {}
        if path.is_file():
            try:
                raw = _json.loads(path.read_text(encoding="utf-8")) or {}
            except (ValueError, OSError):
                raw = {}
        hub_raw = raw.get("hub")
        hub: dict[str, Any] = hub_raw if isinstance(hub_raw, dict) else {}
        hub["operator_key"] = daemon_key
        raw["hub"] = hub
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json.dumps(raw, indent=2), encoding="utf-8")
    except Exception:
        # Best effort: the key is still returned to the caller / printed.
        pass


def _stop_request_path():
    from brains.config import brains_state_dir

    return brains_state_dir() / "daemon.stop"


def _consume_stop_request() -> bool:
    path = _stop_request_path()
    try:
        requested_pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    if requested_pid != os.getpid():
        return False
    with contextlib.suppress(OSError):
        path.unlink()
    return True
