import asyncio

import pytest

from gateahead.snapshots import SnapshotCache


def test_workers_share_one_fetch_and_keep_the_snapshot_after_restart(tmp_path):
    async def run():
        path = tmp_path / "snapshots.db"
        first, second = SnapshotCache(path), SnapshotCache(path)
        calls = 0

        async def fetch():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.02)
            return {"train": "12345", "position": 12}

        answers = await asyncio.gather(*[
            cache.get("live:12345", 90, fetch)
            for cache in [first, second, first, second]
        ])
        assert calls == 1
        assert all(answer == answers[0] for answer in answers)
        first.close()
        second.close()
        restarted = SnapshotCache(path)
        assert await restarted.get("live:12345", 90, fetch) == answers[0]
        assert calls == 1
        restarted.db.execute("UPDATE provider_snapshots SET expires = 0")
        await restarted.get("live:12345", 90, fetch)
        assert calls == 2
        restarted.close()

    asyncio.run(run())


def test_cancelled_request_does_not_cancel_a_shared_provider_fetch(tmp_path):
    async def run():
        cache = SnapshotCache(tmp_path / "snapshots.db")
        started, finish = asyncio.Event(), asyncio.Event()
        calls = 0

        async def fetch():
            nonlocal calls
            calls += 1
            started.set()
            await finish.wait()
            return {"ok": True}

        abandoned = asyncio.create_task(cache.get("train", 90, fetch))
        await started.wait()
        abandoned.cancel()
        with pytest.raises(asyncio.CancelledError):
            await abandoned
        kept = asyncio.create_task(cache.get("train", 90, fetch))
        finish.set()
        assert await kept == {"ok": True}
        assert calls == 1
        cache.close()

    asyncio.run(run())


def test_failed_fetch_releases_the_lease_for_retry(tmp_path):
    async def run():
        cache = SnapshotCache(tmp_path / "snapshots.db")

        async def broken():
            raise RuntimeError("provider unavailable")

        with pytest.raises(RuntimeError):
            await cache.get("train", 90, broken)

        async def recovered():
            return {"ok": True}

        assert await cache.get("train", 90, recovered) == {"ok": True}
        cache.close()

    asyncio.run(run())
