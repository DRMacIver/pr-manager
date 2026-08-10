"""Tests for the run_cmd subprocess wrapper.

Regression: run_cmd awaited proc.communicate() with no timeout, so a
single hung gh/git/tmux invocation froze the poll loop — and with it
every status update — permanently and silently.
"""
from __future__ import annotations

import time

import pytest

from pr_manager.git import run_cmd


@pytest.mark.asyncio
async def test_run_cmd_returns_output():
    rc, out, err = await run_cmd(["echo", "hello"])
    assert rc == 0
    assert out == "hello"


@pytest.mark.asyncio
async def test_run_cmd_check_raises_with_stderr():
    with pytest.raises(RuntimeError, match="boom"):
        await run_cmd(["python3", "-c", "import sys; sys.exit('boom')"])


@pytest.mark.asyncio
async def test_run_cmd_check_false_returns_nonzero():
    rc, _out, err = await run_cmd(
        ["python3", "-c", "import sys; sys.exit('boom')"], check=False,
    )
    assert rc != 0
    assert "boom" in err


@pytest.mark.asyncio
async def test_run_cmd_times_out_and_kills_the_process():
    start = time.monotonic()
    rc, _out, err = await run_cmd(
        ["sleep", "600"], check=False, timeout=0.2,
    )
    elapsed = time.monotonic() - start
    assert elapsed < 5, "run_cmd must not wait for the hung process"
    assert rc != 0
    assert "timed out" in err.lower()


@pytest.mark.asyncio
async def test_run_cmd_timeout_with_check_raises():
    with pytest.raises(RuntimeError, match="timed out"):
        await run_cmd(["sleep", "600"], timeout=0.2)
