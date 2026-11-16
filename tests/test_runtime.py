"""One-host deployment: shared environment, fail-fast children and bounded shutdown."""
import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path
import sys
import time
from unittest.mock import AsyncMock

import pytest

from gateahead.config import load_settings
from gateahead.runtime import runtime_config, service_commands, supervise


def test_runtime_pins_one_database_path_and_respects_the_platform_port(tmp_path, monkeypatch):
    settings = replace(load_settings(), data_dir=tmp_path / "volume")
    monkeypatch.setenv("GATEAHEAD_DATA", "/a/different/old/path")
    summary, environment = runtime_config(settings, "0.0.0.0", 10000, False)
    assert summary["services"] == ["api", "worker"]
    assert environment["GATEAHEAD_DATA"] == str(settings.data_dir.resolve())
    assert environment["GATEAHEAD_WORKER_REQUIRED"] == "1"
    assert summary["database"] == str(settings.db_path)
    assert runtime_config(settings, "0.0.0.0", 10000, False)[0]["database_id"] == summary["database_id"]
    assert service_commands("0.0.0.0", 10000, False)["api"][-3:] == ["10000", "--timeout-graceful-shutdown", "10"]
    assert "bot" not in service_commands("0.0.0.0", 10000, False)


def test_bot_is_explicit_and_configuration_output_does_not_expose_its_token(tmp_path):
    settings = replace(load_settings(), data_dir=tmp_path, telegram_bot_token="")
    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        runtime_config(settings, "0.0.0.0", 10000, True)
    configured = replace(settings, telegram_bot_token="example-private-value")
    summary, _ = runtime_config(configured, "0.0.0.0", 10000, True)
    assert summary["services"] == ["api", "worker", "bot"]
    assert configured.telegram_bot_token not in json.dumps(summary)


async def await_file(path):
    async with asyncio.timeout(5):
        while not path.exists():
            await asyncio.sleep(0.01)


def test_lost_worker_stops_the_api_and_returns_failure_for_host_restart(tmp_path):
    api_ready, worker_seen = tmp_path / "api-ready", tmp_path / "worker-env"
    # Real subprocesses communicate through files, without local TCP permissions.
    api = [sys.executable, "-c", "import pathlib, time, os; pathlib.Path(os.environ['READY']).touch(); time.sleep(30)"]
    worker = [sys.executable, "-c", "import pathlib, time, os; p=pathlib.Path(os.environ['READY']); "
              "exec('while not p.exists(): time.sleep(.01)'); pathlib.Path(os.environ['SEEN']).write_text(os.environ['GATEAHEAD_DATA']); raise SystemExit(7)"]

    async def run():
        stop = asyncio.Event()
        result = await asyncio.wait_for(supervise({"api": api, "worker": worker},
                {**os.environ, "READY": str(api_ready), "SEEN": str(worker_seen), "GATEAHEAD_DATA": str(tmp_path)}, stop, 1), 6)
        assert result == 7
        assert worker_seen.read_text() == str(tmp_path)

    asyncio.run(run())


def test_clean_worker_exit_still_requires_host_restart(monkeypatch):
    class Process:
        returncode = 0
        pid = 123
        wait = AsyncMock(return_value=0)
    monkeypatch.setattr("gateahead.runtime.asyncio.create_subprocess_exec", AsyncMock(return_value=Process()))
    assert asyncio.run(supervise({"worker": ["example"]}, {}, asyncio.Event())) == 1


def test_shutdown_signal_stops_all_children_and_kills_a_stuck_child(tmp_path):
    ready = tmp_path / "started"
    # This child ignores SIGTERM, requiring the bounded hard-stop path.
    script = "import pathlib,signal,time,os; signal.signal(signal.SIGTERM,signal.SIG_IGN); pathlib.Path(os.environ['READY']).touch(); time.sleep(30)"
    async def run():
        stop = asyncio.Event()
        task = asyncio.create_task(supervise({"api": [sys.executable, "-c", script]},
                {**os.environ, "READY": str(ready)}, stop, 0.1))
        await await_file(ready)
        stop.set()
        assert await asyncio.wait_for(task, 3) == 0
    asyncio.run(run())


def test_failed_second_spawn_closes_the_first_child(monkeypatch):
    class Process:
        returncode = None
        pid = 123
        def terminate(self): self.returncode = -15
        async def wait(self):
            while self.returncode is None:
                await asyncio.sleep(0)
            return self.returncode
    first = Process()
    monkeypatch.setattr("gateahead.runtime.asyncio.create_subprocess_exec", AsyncMock(side_effect=[first, OSError("spawn failed")]))
    with pytest.raises(OSError, match="spawn failed"):
        asyncio.run(supervise({"api": ["example"], "worker": ["missing"]}, {}, asyncio.Event(), 0.1))
    assert first.returncode == -15
