import asyncio
from dataclasses import replace

import pytest

from reach_bridge.runner import BridgeError, Runner

from .conftest import pid_running, wait_for_file


@pytest.mark.parametrize("query", ["large", "stderr"])
async def test_output_limit_and_slot_release(settings, query):
    runner = Runner(replace(settings, max_output_bytes=1024))
    with pytest.raises(BridgeError, match="大小上限"):
        await asyncio.wait_for(runner.run(["bilibili", "search", query]), 5)
    assert not runner.processes
    assert (await runner.run(["version"]))["ok"] is True


async def test_timeout_and_busy(settings):
    runner = Runner(replace(settings, timeout=1, queue_timeout=0.05, concurrency=1))
    task = asyncio.create_task(runner.run(["bilibili", "search", "slow"]))
    await asyncio.sleep(0.1)
    with pytest.raises(BridgeError) as busy:
        await runner.run(["version"])
    assert busy.value.code == "BUSY"
    with pytest.raises(BridgeError) as timeout:
        await task
    assert timeout.value.code == "TIMEOUT"
    assert not runner.processes
    assert (await runner.run(["version"]))["ok"] is True


@pytest.mark.parametrize("shutdown", [False, True, "during_cancel"])
async def test_cancellation_reaps_request_descendants(settings, monkeypatch, tmp_path, shutdown):
    pid_file = tmp_path / "child.pid"
    monkeypatch.setenv("FIXTURE_PID_FILE", str(pid_file))
    runner = Runner(settings)
    task = asyncio.create_task(runner.run(["bilibili", "search", "spawn"]))
    await wait_for_file(pid_file)
    pid = int(pid_file.read_text())
    assert pid_running(pid)
    if shutdown == "during_cancel":
        task.cancel()
        await runner.close()
    elif shutdown:
        await runner.close()
    else:
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not pid_running(pid)
    assert not runner.processes
    if shutdown:
        with pytest.raises(BridgeError) as error:
            await runner.run(["version"])
        assert error.value.code == "STOPPING"


async def test_missing_program_is_visible(settings):
    with pytest.raises(BridgeError) as error:
        await Runner(replace(settings, executable="/not-installed/agent-reach")).run(["version"])
    assert error.value.code == "NEED_SETUP"
