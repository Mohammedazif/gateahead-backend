"""Run the API and catalog worker on one host with one persistent SQLite database."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from collections.abc import Mapping, Sequence

from gateahead.config import Settings, load_settings
from gateahead.store import Store

_log = logging.getLogger(__name__)


def service_commands(host: str, port: int, with_bot: bool) -> dict[str, list[str]]:
    commands = {
        "api": [sys.executable, "-m", "uvicorn", "gateahead.api.app:app", "--host", host,
                "--port", str(port), "--timeout-graceful-shutdown", "10"],
        "worker": [sys.executable, "-m", "gateahead.catalog", "sync", "--watch", "--interval-seconds", "60"],
    }
    if with_bot:
        commands["bot"] = [sys.executable, "-m", "gateahead.bot"]
    return commands


async def supervise(commands: Mapping[str, Sequence[str]], environment: dict[str, str], stop: asyncio.Event,
                    shutdown_seconds: float = 15) -> int:
    """A lost required child stops the whole instance; the host can then restart it."""
    processes: dict[str, asyncio.subprocess.Process] = {}
    waits: dict[asyncio.Task, str] = {}
    stop_wait = asyncio.create_task(stop.wait())
    try:
        for name, command in commands.items():
            processes[name] = await asyncio.create_subprocess_exec(*command, env=environment)
            _log.info("Started %s pid=%s", name, processes[name].pid)
            waits[asyncio.create_task(processes[name].wait())] = name
        done, _ = await asyncio.wait([stop_wait, *waits], return_when=asyncio.FIRST_COMPLETED)
        if stop_wait in done:
            return 0
        name = waits[next(task for task in done if task in waits)]
        code = processes[name].returncode
        _log.error("Required service %s exited with code %s; stopping this instance", name, code)
        return code if code and code > 0 else 1
    finally:
        for process in processes.values():
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
        try:
            await asyncio.wait_for(asyncio.gather(*(p.wait() for p in processes.values())), shutdown_seconds)
        except TimeoutError:
            for process in processes.values():
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
            await asyncio.gather(*(p.wait() for p in processes.values()))
        for task in [stop_wait, *waits]:
            task.cancel()
        await asyncio.gather(stop_wait, *waits, return_exceptions=True)


def runtime_config(settings: Settings, host: str, port: int, with_bot: bool) -> tuple[dict, dict[str, str]]:
    if with_bot and (not settings.telegram_bot_token or settings.telegram_bot_token.startswith("YOUR_")):
        raise ValueError("Configure TELEGRAM_BOT_TOKEN before enabling the bot")
    store = Store(settings.db_path)
    try:
        identity = store.meta("database_id")
    finally:
        store._conn.close()
    environment = {**os.environ, "GATEAHEAD_DATA": str(settings.data_dir.resolve()),
                   "GATEAHEAD_WORKER_REQUIRED": "1", "PYTHONUNBUFFERED": "1"}
    return {"database": str(settings.db_path.resolve()), "database_id": identity, "host": host, "port": port,
            "services": list(service_commands(host, port, with_bot))}, environment


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=os.getenv("PORT", "8000"))
    parser.add_argument("--with-bot", action="store_true",
                        default=os.getenv("GATEAHEAD_ENABLE_BOT", "").lower() in {"1", "true", "yes"})
    parser.add_argument("--check-config", action="store_true", help="Validate shared storage and show planned services without starting them")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    settings = load_settings()
    try:
        summary, environment = runtime_config(settings, args.host, args.port, args.with_bot)
    except ValueError as exc:
        parser.error(str(exc))
    if args.check_config:
        print(json.dumps(summary), flush=True)
        return
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    _log.info("Shared runtime %s", json.dumps(summary))

    async def run() -> int:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stop.set)
        try:
            return await supervise(service_commands(args.host, args.port, args.with_bot), environment, stop)
        finally:
            for signum in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(signum)

    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
