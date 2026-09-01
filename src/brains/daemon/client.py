"""Typed HTTP client to the hub (WS1 §8.1).

Thin wrapper over :mod:`httpx`. Authenticates with the operator/relay bearer key
exactly like every other ``/v1/*`` client — no new auth code. Tests may inject a
pre-built ``httpx.Client`` (e.g. backed by ``ASGITransport``) via ``http=``.
"""

from __future__ import annotations

from typing import Any

import httpx


class HubClient:
    def __init__(
        self,
        base_url: str,
        operator_key: str,
        *,
        verify_tls: bool = True,
        http: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.operator_key = operator_key
        headers = {"Authorization": f"Bearer {operator_key}"} if operator_key else {}
        if http is not None:
            # Make sure an injected client still carries auth.
            http.headers.update(headers)
            self._http = http
            self._owns_http = False
        else:
            self._http = httpx.Client(
                base_url=self.base_url,
                headers=headers,
                verify=verify_tls,
                timeout=timeout,
            )
            self._owns_http = True

    # --- lifecycle -------------------------------------------------------- #

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> HubClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _json(self, resp: httpx.Response) -> dict:
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {}

    # --- registration + liveness ----------------------------------------- #

    def register(self, payload: dict[str, Any]) -> dict:
        return self._json(self._http.post("/v1/runtimes/register", json=payload))

    def redeem_enrolment(
        self, token: str, machine_id: str, clis: list[dict], *, org_id: int | None = None
    ) -> dict:
        """Redeem an enrolment token (Connect a machine, F1.2).

        Intentionally needs **no** operator key — the token is the credential, so
        this hits the unauthenticated ``/v1/runtimes/enrol/redeem`` route. The hub
        registers one runtime per CLI and returns them.
        """
        return self._json(
            self._http.post(
                "/v1/runtimes/enrol/redeem",
                json={
                    "token": token,
                    "machine_id": machine_id,
                    "clis": clis,
                    "org_id": org_id,
                },
            )
        )

    def heartbeat_batch(self, machine_id: str, runtimes: list[dict]) -> dict:
        return self._json(
            self._http.post(
                "/v1/runtimes/heartbeat",
                json={"machine_id": machine_id, "runtimes": runtimes},
            )
        )

    def heartbeat(self, runtime_id: int | str, **body: Any) -> dict:
        return self._json(self._http.post(f"/v1/runtimes/{runtime_id}/heartbeat", json=body))

    def list_runtimes(self, **filters: Any) -> list[dict]:
        params = {k: v for k, v in filters.items() if v is not None}
        data = self._json(self._http.get("/v1/runtimes", params=params))
        return data.get("runtimes", [])

    def deregister(self, runtime_id: int | str) -> dict:
        return self._json(self._http.delete(f"/v1/runtimes/{runtime_id}"))

    # --- assignments ------------------------------------------------------ #

    def get_assignments(self, runtime_id: int | str) -> list[dict]:
        data = self._json(self._http.get(f"/v1/runtimes/{runtime_id}/assignments"))
        return data.get("assignments", [])

    def get_help_reviews(self, runtime_id: int | str) -> list[dict]:
        data = self._json(self._http.get(f"/v1/runtimes/{runtime_id}/help-reviews"))
        return data.get("reviews", [])

    def claim_help_review(self, runtime_id: int | str, code: str) -> dict:
        return self._json(self._http.post(f"/v1/runtimes/{runtime_id}/help-reviews/{code}/claim"))

    def complete_help_review(
        self,
        runtime_id: int | str,
        code: str,
        **body: Any,
    ) -> dict:
        return self._json(
            self._http.post(
                f"/v1/runtimes/{runtime_id}/help-reviews/{code}/complete",
                json=body,
            )
        )

    def claim(self, runtime_id: int | str, assignment_id: str) -> dict:
        return self._json(
            self._http.post(f"/v1/runtimes/{runtime_id}/assignments/{assignment_id}/claim")
        )

    def ack(
        self,
        runtime_id: int | str,
        assignment_id: str,
        state: str,
        *,
        session_id: str | None = None,
        returncode: int | None = None,
    ) -> dict:
        return self._json(
            self._http.post(
                f"/v1/runtimes/{runtime_id}/assignments/{assignment_id}/ack",
                json={
                    "state": state,
                    "session_id": session_id,
                    "returncode": returncode,
                },
            )
        )

    # --- sessions + events ------------------------------------------------ #

    def open_session(self, runtime_id: int | str, **body: Any) -> dict:
        return self._json(self._http.post(f"/v1/runtimes/{runtime_id}/sessions", json=body))

    def post_event(
        self,
        runtime_id: int | str,
        session_id: str,
        *,
        seq: int,
        stream: str,
        chunk: str,
        ts: str | None = None,
        exec_id: str | None = None,
    ) -> dict:
        return self._json(
            self._http.post(
                f"/v1/runtimes/{runtime_id}/sessions/{session_id}/events",
                json={
                    "seq": seq,
                    "stream": stream,
                    "chunk": chunk,
                    "ts": ts,
                    "exec_id": exec_id,
                },
            )
        )

    def set_session_state(self, session_id: str, state: str, *, summary: str | None = None) -> dict:
        """Report a Session's lifecycle state to the hub (F3.2, BL-P0-05).

        The daemon owns the process, so it is the only party that can tell the
        hub the Session is running or has finished. Without this the hub keeps
        showing a Session as live long after its agent exited, which is the
        hub/local divergence BL-P0-05 exists to close.
        """
        return self._json(
            self._http.post(
                f"/v1/sessions/{session_id}/state",
                json={"state": state, "summary": summary},
            )
        )

    # --- session commands (BL-P0-05) -------------------------------------- #

    def get_session_commands(self, runtime_id: int | str, *, limit: int = 25) -> list[dict]:
        data = self._json(
            self._http.get(f"/v1/runtimes/{runtime_id}/session-commands", params={"limit": limit})
        )
        return data.get("commands", [])

    def claim_session_command(self, runtime_id: int | str, command_id: str) -> dict:
        return self._json(
            self._http.post(f"/v1/runtimes/{runtime_id}/session-commands/{command_id}/claim")
        )

    def release_session_command(
        self, runtime_id: int | str, command_id: str, *, reason: str | None = None
    ) -> dict:
        """Hand a claimed command back, for one this Runtime does not own."""
        return self._json(
            self._http.post(
                f"/v1/runtimes/{runtime_id}/session-commands/{command_id}/release",
                json={"reason": reason},
            )
        )

    def ack_session_command(
        self,
        runtime_id: int | str,
        command_id: str,
        *,
        result: str,
        ok: bool = True,
        error: str | None = None,
    ) -> dict:
        return self._json(
            self._http.post(
                f"/v1/runtimes/{runtime_id}/session-commands/{command_id}/ack",
                json={"result": result, "ok": ok, "error": error},
            )
        )

    def reconcile_sessions(
        self,
        runtime_id: int | str,
        owned_session_ids: list[str],
        *,
        reason: str | None = None,
    ) -> dict:
        return self._json(
            self._http.post(
                f"/v1/runtimes/{runtime_id}/sessions/reconcile",
                json={"owned_session_ids": owned_session_ids, "reason": reason},
            )
        )
