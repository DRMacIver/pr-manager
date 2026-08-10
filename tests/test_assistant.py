"""Tests for the in-TUI chat assistant's executable core."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from pr_manager.assistant import Assistant


def _assistant() -> Assistant:
    return Assistant(ctx=MagicMock())


def test_default_model_is_current():
    """Regression: the default was claude-sonnet-4-20250514, which is past
    its retirement date — every chat request 404'd, so the entire feature
    was dead on arrival."""
    assert _assistant().model == "claude-sonnet-5"


@pytest.mark.asyncio
async def test_exec_captures_print():
    out = await _assistant()._exec_python("print('hello')")
    assert "hello" in out


@pytest.mark.asyncio
async def test_exec_returns_last_expression():
    out = await _assistant()._exec_python("1 + 1")
    assert "2" in out


@pytest.mark.asyncio
async def test_exec_supports_await():
    out = await _assistant()._exec_python(
        "import asyncio\nawait asyncio.sleep(0)\n'awaited'"
    )
    assert "awaited" in out


@pytest.mark.asyncio
async def test_exec_returns_traceback_on_error():
    out = await _assistant()._exec_python("1 / 0")
    assert "ZeroDivisionError" in out


@pytest.mark.asyncio
async def test_exec_no_output():
    out = await _assistant()._exec_python("x = 1")
    assert out == "(no output)"
