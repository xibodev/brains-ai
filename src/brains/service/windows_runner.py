"""Task-owned recovery for the Windows serve-all supervisor.

The XML bootstrap binds state before importing this package. Task Scheduler owns
this action's lifetime; service stop ends the action before cleaning the captured
supervisor PID tree. Only the supervisor writes the service PID record.
"""

from __future__ import annotations

import argparse
import ntpath
import subprocess
import sys
import time

RESTART_INTERVAL_SECONDS = 60
RESTART_COUNT = 9999


def validate_args(args: list[str]) -> None:
    """Accept only foreground serve-all, never an alternate Python/Brains command."""
    if (
        not args
        or not all(isinstance(arg, str) and "\0" not in arg for arg in args)
        or args[0] != "serve-all"
    ):
        raise ValueError("Windows runner requires foreground serve-all arguments")
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False, exit_on_error=False)
    parser.add_argument("--gateway-host")
    for option in ("--gateway-port", "--mcp-port", "--mcp-scheduler-interval"):
        parser.add_argument(option, type=int)
    for option in ("--no-gateway", "--no-mcp"):
        parser.add_argument(option, action="store_true")
    try:
        _, unknown = parser.parse_known_args(args[1:])
    except argparse.ArgumentError:
        raise ValueError("Invalid Windows serve-all arguments") from None
    if unknown:
        raise ValueError("Invalid Windows serve-all arguments")


def main(args: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if args is None else args)
    if (
        not args
        or not isinstance(args[0], str)
        or "\0" in args[0]
        or not ntpath.isabs(args[0])
        or ntpath.basename(args[0]).casefold() != "pythonw.exe"
    ):
        return 2
    try:
        validate_args(args[1:])
    except ValueError:
        return 2
    # Do not substitute sys.executable: a venv redirector may expose base Python.
    command = [args[0], "-m", "brains", *args[1:]]
    for attempt in range(RESTART_COUNT + 1):
        try:
            child = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            # Launch failures consume the same budget without exposing paths/env.
            result = 1
        else:
            # A failed wait or cancellation is fatal, not permission to spawn a
            # second child whose predecessor may still be alive.
            result = child.wait()
        if result == 0:
            return 0
        if attempt < RESTART_COUNT:
            time.sleep(RESTART_INTERVAL_SECONDS)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
