"""Tests for Milestone 3 R66: worker self-heartbeat check, trimmed /health, and JobInfo.expires_at."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import redis.exceptions as redis_exc
from fastapi.testclient import TestClient

from downloader.job_manager import JobInfo, JobManager, JobStatus
from downloader.worker import run_healthcheck
from downloader.worker_queue import WORKER_HEARTBEAT_PREFIX, WorkerQueue


@pytest.mark.unit
class TestHealthEndpointSanitizedR66:
    """Ensure unauthenticated /health does not leak hostnames, queue depths, raw errors, or scan Redis."""

    def test_health_does_not_scan_workers_or_leak_hostnames_queues_or_raw_errors(
        self, api_client: TestClient, env_no_redis: None
    ) -> None:
        from downloader.main import app

        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(
            side_effect=redis_exc.ConnectionError("secret-redis-host:6379 refused")
        )
        mock_jm = MagicMock()
        mock_jm.redis_client = mock_redis

        mock_queue = MagicMock()
        mock_queue.list_active_workers = AsyncMock(
            return_value=[{"worker_id": "prod-node-1-abcd1234", "hostname": "prod-node-1"}]
        )
        mock_queue.get_queue_depths = AsyncMock(return_value={"batch_pending": 42})

        orig_jm = getattr(app.state, "job_manager", None)
        orig_wq = getattr(app.state, "worker_queue", None)
        app.state.job_manager = mock_jm
        app.state.worker_queue = mock_queue
        try:
            response = api_client.get("/health")
        finally:
            app.state.job_manager = orig_jm
            app.state.worker_queue = orig_wq

        assert response.status_code == 200
        data = response.json()
        jm_status = data["services"]["job_manager"]
        assert jm_status["status"] == "error"
        assert "error" not in jm_status
        assert "secret-redis-host" not in response.text

        workers_status = data["services"]["workers"]
        assert workers_status["queue_configured"] is True
        assert "workers" not in workers_status
        assert "queues" not in workers_status
        assert "active_workers" not in workers_status
        assert "prod-node-1" not in response.text
        mock_queue.list_active_workers.assert_not_called()
        mock_queue.get_queue_depths.assert_not_called()


@pytest.mark.unit
class TestWorkerHealthcheckR66:
    """Ensure worker --healthcheck verifies this process's own heartbeat key."""

    @pytest.mark.asyncio
    async def test_healthcheck_checks_own_worker_id_not_any_worker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        id_file = tmp_path / "worker.id"
        id_file.write_text("worker-self-1234", encoding="utf-8")
        monkeypatch.setenv("WORKER_ID_FILE", str(id_file))
        monkeypatch.delenv("WORKER_ID", raising=False)

        mock_settings = MagicMock()
        mock_settings.redis.redis_uri = "redis://localhost:6379"

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)

        mock_jm = MagicMock()
        mock_jm.redis_client = mock_redis
        mock_jm.connect = AsyncMock()
        mock_jm.disconnect = AsyncMock()

        with (
            patch("downloader.worker.get_settings", return_value=mock_settings),
            patch("downloader.worker.JobManager", return_value=mock_jm),
        ):
            rc_missing = await run_healthcheck()
            assert rc_missing == 1
            mock_redis.get.assert_awaited_once_with(f"{WORKER_HEARTBEAT_PREFIX}worker-self-1234")

            mock_redis.get = AsyncMock(return_value=b'{"status":"alive"}')
            rc_ok = await run_healthcheck()
            assert rc_ok == 0
            mock_redis.get.assert_awaited_once_with(f"{WORKER_HEARTBEAT_PREFIX}worker-self-1234")

    @pytest.mark.asyncio
    async def test_healthcheck_fails_when_no_local_worker_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WORKER_ID_FILE", str(tmp_path / "missing.id"))
        monkeypatch.delenv("WORKER_ID", raising=False)

        mock_settings = MagicMock()
        mock_settings.redis.redis_uri = "redis://localhost:6379"

        with patch("downloader.worker.get_settings", return_value=mock_settings):
            rc = await run_healthcheck()
        assert rc == 1

    @pytest.mark.asyncio
    async def test_worker_queue_has_heartbeat(self) -> None:
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=b'{"status":"alive"}')
        queue = WorkerQueue(mock_redis)
        assert await queue.has_heartbeat("w-1") is True
        mock_redis.get.assert_awaited_once_with(f"{WORKER_HEARTBEAT_PREFIX}w-1")


@pytest.mark.unit
class TestJobInfoExpiresAtRenewalR66:
    """Ensure JobInfo.expires_at is renewed when update_job_status extends Redis TTL."""

    @pytest.mark.asyncio
    async def test_update_job_status_renews_expires_at(self) -> None:
        old_created = datetime.now(UTC) - timedelta(hours=12)
        old_expires = old_created + timedelta(seconds=3600)
        initial = JobInfo(
            job_id="job-ttl-1",
            status=JobStatus.PENDING,
            created_at=old_created,
            total_urls=1,
            request_data={"urls": ["https://example.com"]},
            expires_at=old_expires,
        )
        stored_payloads: dict[str, str] = {"job:job-ttl-1": initial.model_dump_json()}

        class _FakePipe:
            def __init__(self) -> None:
                self._ops: list[tuple[str, str]] = []

            async def __aenter__(self) -> _FakePipe:
                return self

            async def __aexit__(self, *args: Any) -> None:
                return None

            async def watch(self, _key: str) -> None:
                return None

            def multi(self) -> None:
                return None

            async def setex(self, key: str, _ttl: int, value: str) -> None:
                self._ops.append((key, value))

            async def execute(self) -> None:
                for key, value in self._ops:
                    stored_payloads[key] = value

        mock_redis = MagicMock()
        mock_redis.get = AsyncMock(side_effect=lambda k: stored_payloads.get(k))
        mock_redis.pipeline = MagicMock(return_value=_FakePipe())

        jm = JobManager("redis://localhost:6379", job_ttl=3600)
        jm.redis_client = mock_redis

        await asyncio.sleep(0.01)
        await jm.update_job_status("job-ttl-1", JobStatus.RUNNING)

        updated = JobInfo.model_validate_json(stored_payloads["job:job-ttl-1"])
        assert updated.status == JobStatus.RUNNING
        assert updated.expires_at is not None
        assert updated.expires_at > old_expires
