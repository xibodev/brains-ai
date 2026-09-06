"""Containment regressions for withdrawn automatic peer execution."""

from __future__ import annotations

import pytest

from brains.control.help import file_help_request
from brains.control.help_execution import (
    dispatch_due_help_reviews,
    run_local_review,
    schedule_help_review,
)


@pytest.mark.parametrize("mode", ["auto", "ephemeral"])
def test_file_help_rejects_withdrawn_execution_modes(mode: str) -> None:
    with pytest.raises(ValueError, match="withdrawn"):
        file_help_request(
            "review",
            "inspect",
            to_workspace="historical-workspace",
            required_tool="codex",
            execution_mode=mode,
        )


def test_background_review_entry_points_fail_closed() -> None:
    assert schedule_help_review("ASK-0001") is False
    assert dispatch_due_help_reviews() == []
    with pytest.raises(ValueError, match="withdrawn"):
        run_local_review("ASK-0001")
