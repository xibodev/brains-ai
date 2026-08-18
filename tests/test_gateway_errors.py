"""Tests for :mod:`brains.gateway.errors`."""

from __future__ import annotations

from fastapi import HTTPException

from brains.gateway.errors import bad_request


def test_bad_request_returns_400_http_exception() -> None:
    exc = bad_request("missing field 'model'")
    assert isinstance(exc, HTTPException)
    assert exc.status_code == 400
    assert exc.detail == "missing field 'model'"
