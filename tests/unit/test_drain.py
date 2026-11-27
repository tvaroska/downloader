"""Unit tests for graceful shutdown, pre-stop readiness, and connection draining."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from downloader.config import Settings
from downloader.dependencies import (
    get_http_client,
    get_robots_checker,
)
from downloader.drain import DrainCoordinator, install_sigterm_drain_handler
from downloader.job_manager import JobManager
from downloader.main import app


class TestGracefulShutdownAndDrain:
    async def test_drain_coordinator_waits_and_times_out(self) -> None:
        coord = DrainCoordinator()
        assert await coord.wait_until_drained(0.1) is True

        async def _slow_req() -> None:
            async with coord.track_request():
                await asyncio.sleep(0.05)

        task = asyncio.create_task(_slow_req())
        await asyncio.sleep(0.01)
        coord.start_drain()
        assert coord.is_draining is True
        assert coord.active_requests == 1

        # Short timeout returns False while request is still active
        assert await coord.wait_until_drained(0.005) is False
        # Waiting long enough completes cleanly
        assert await coord.wait_until_drained(1.0) is True
        await task
        assert coord.active_requests == 0

    def test_drain_middleware_and_health_return_503_while_draining(self) -> None:
        with TestClient(app) as client:
            ok = client.get("/health")
            assert ok.status_code == 200
            assert ok.json()["status"] == "healthy"
            assert ok.json()["draining"] is False

            app.state.drain_coordinator.start_drain()
            try:
                draining_health = client.get("/health")
                assert draining_health.status_code == 503
                assert draining_health.json()["status"] == "draining"
                assert draining_health.json()["draining"] is True

                rejected = client.get("/https://example.com")
                assert rejected.status_code == 503
                assert rejected.json()["detail"]["error_type"] == "service_draining"
                assert rejected.headers["connection"] == "close"
                assert rejected.headers["retry-after"] == "5"
            finally:
                app.state.drain_coordinator.reset()

    async def test_job_manager_wait_for_active_jobs(self) -> None:
        jm = JobManager("redis://localhost:6379")
        assert await jm.wait_for_active_jobs(0.1) is True

        async def _work() -> None:
            await asyncio.sleep(0.04)

        jm._background_tasks["job-1"] = asyncio.create_task(_work())
        assert await jm.wait_for_active_jobs(0.005) is False
        assert await jm.wait_for_active_jobs(1.0) is True

    async def test_middleware_counts_in_flight_requests(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def _slow_download(*_args, **_kwargs):
            started.set()
            await release.wait()
            return (
                b"<html><body>ok</body></html>",
                {
                    "url": "https://example.com",
                    "status_code": 200,
                    "content_type": "text/html",
                    "size": 28,
                    "headers": {"content-type": "text/html"},
                },
            )

        mock_http = AsyncMock()
        mock_http.download = _slow_download
        mock_http.close = AsyncMock()
        mock_robots = AsyncMock()
        mock_robots.check_allowed = AsyncMock(return_value=None)
        mock_robots.close = AsyncMock()

        app.dependency_overrides[get_http_client] = lambda: mock_http
        app.dependency_overrides[get_robots_checker] = lambda: mock_robots
        try:
            async with app.router.lifespan_context(app):
                app.state.drain_coordinator.reset()
                app.state.settings = Settings()
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    inflight = asyncio.create_task(client.get("/https://example.com"))
                    await asyncio.wait_for(started.wait(), timeout=2.0)

                    assert app.state.drain_coordinator.active_requests == 1
                    assert await app.state.drain_coordinator.wait_until_drained(0.01) is False

                    app.state.drain_coordinator.start_drain()
                    rejected = await client.get("/https://example.com")
                    assert rejected.status_code == 503
                    assert rejected.json()["detail"]["error_type"] == "service_draining"

                    release.set()
                    ok_resp = await inflight
                    assert ok_resp.status_code == 200
                    assert app.state.drain_coordinator.active_requests == 0
                    assert await app.state.drain_coordinator.wait_until_drained(1.0) is True
                app.state.drain_coordinator.reset()
        finally:
            app.dependency_overrides.pop(get_http_client, None)
            app.dependency_overrides.pop(get_robots_checker, None)

    async def test_lifespan_shutdown_drains_before_closing_clients(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from downloader.main import _teardown_services

        events: list[str] = []
        coord = DrainCoordinator()
        orig_start = coord.start_drain
        orig_wait = coord.wait_until_drained

        def _tracked_start() -> None:
            events.append("start_drain")
            orig_start()

        async def _tracked_wait(timeout: float) -> bool:
            events.append("wait_until_drained")
            return await orig_wait(timeout)

        monkeypatch.setattr(coord, "start_drain", _tracked_start)
        monkeypatch.setattr(coord, "wait_until_drained", _tracked_wait)

        fake_http = AsyncMock()
        fake_http.close = AsyncMock(side_effect=lambda: events.append("http_client.close"))
        fake_robots = AsyncMock()
        fake_robots.close = AsyncMock(side_effect=lambda: events.append("robots_checker.close"))
        fake_jm = AsyncMock()
        fake_jm.wait_for_active_jobs = AsyncMock(
            side_effect=lambda _t: events.append("wait_for_active_jobs")
        )
        fake_jm.disconnect = AsyncMock(side_effect=lambda: events.append("job_manager.disconnect"))
        fake_wq = AsyncMock()
        fake_wq.close = AsyncMock(side_effect=lambda: events.append("worker_queue.close"))
        fake_metrics = AsyncMock()
        fake_metrics.stop = AsyncMock(side_effect=lambda: events.append("metrics.stop"))

        fake_app = MagicMock()
        fake_app.state.job_manager = fake_jm
        fake_app.state.worker_queue = fake_wq
        fake_app.state.scheduler = None
        fake_app.state.robots_checker = fake_robots
        fake_app.state.pdf_generator = None

        await _teardown_services(fake_app, Settings(), coord, fake_metrics, fake_http)
        assert coord.is_draining is False
        assert events[:2] == ["start_drain", "wait_until_drained"]
        assert events.index("wait_until_drained") < events.index("wait_for_active_jobs")
        assert events.index("wait_for_active_jobs") < events.index("http_client.close")
        assert events.index("job_manager.disconnect") < events.index("http_client.close")
        assert events.index("wait_until_drained") < events.index("robots_checker.close")
        assert "worker_queue.close" in events

    def test_sigterm_handler_triggers_drain_and_chains_previous(self) -> None:
        coord = DrainCoordinator()
        chained: list[int] = []
        orig_handler = signal.getsignal(signal.SIGTERM)

        def _prev(signum: int, _frame: object) -> None:
            chained.append(signum)

        signal.signal(signal.SIGTERM, _prev)
        try:
            restore = install_sigterm_drain_handler(coord)
            try:
                assert coord.is_draining is False
                os.kill(os.getpid(), signal.SIGTERM)
                assert coord.is_draining is True
                assert chained == [signal.SIGTERM]
            finally:
                restore()
            assert signal.getsignal(signal.SIGTERM) is _prev
        finally:
            signal.signal(signal.SIGTERM, orig_handler)

    async def test_health_503_is_observable_for_a_probe_period_after_sigterm(self) -> None:
        """`S-8`: SIGTERM fails `/health` (`503`) during `pre_stop_delay` while still serving live requests (`200`)."""
        coord = DrainCoordinator()
        chained_at: list[float] = []

        def _prev(_signum: int, _frame: object) -> None:
            chained_at.append(time.monotonic())

        mock_http = AsyncMock()
        mock_http.download = AsyncMock(
            return_value=(
                b"<html><body>still-serving</body></html>",
                {
                    "url": "https://example.com",
                    "status_code": 200,
                    "content_type": "text/html",
                    "size": 39,
                    "headers": {"content-type": "text/html"},
                },
            )
        )
        mock_robots = AsyncMock()
        mock_robots.check_allowed = AsyncMock(return_value=None)
        app.dependency_overrides[get_http_client] = lambda: mock_http
        app.dependency_overrides[get_robots_checker] = lambda: mock_robots
        try:
            async with app.router.lifespan_context(app):
                orig_handler = signal.getsignal(signal.SIGTERM)
                signal.signal(signal.SIGTERM, _prev)
                prev_coord = app.state.drain_coordinator
                app.state.drain_coordinator = coord
                try:
                    restore = install_sigterm_drain_handler(coord, pre_stop_delay=0.15)
                    try:
                        sent_at = time.monotonic()
                        os.kill(os.getpid(), signal.SIGTERM)
                        # Pre-stop window fails readiness (`is_shutting_down`) without rejecting live requests yet!
                        assert coord.is_shutting_down is True
                        assert coord.is_draining is False
                        assert chained_at == []

                        transport = httpx.ASGITransport(app=app)
                        async with httpx.AsyncClient(
                            transport=transport, base_url="http://test"
                        ) as client:
                            probe = await client.get("/health")
                            assert probe.status_code == 503
                            assert probe.json()["status"] == "draining"

                            with patch(
                                "downloader.routes.download.validate_url_async",
                                new=AsyncMock(return_value="https://example.com"),
                            ):
                                live_ok = await client.get("/https://example.com")
                            assert live_ok.status_code == 200
                            assert chained_at == []

                            await asyncio.sleep(0.2)
                            assert len(chained_at) == 1
                            assert (chained_at[0] - sent_at) >= 0.12
                            assert coord.is_draining is True
                            rejected = await client.get("/https://example.com")
                            assert rejected.status_code == 503
                            assert rejected.json()["detail"]["error_type"] == "service_draining"
                    finally:
                        restore()
                finally:
                    app.state.drain_coordinator = prev_coord
                    signal.signal(signal.SIGTERM, orig_handler)
        finally:
            app.dependency_overrides.pop(get_http_client, None)
            app.dependency_overrides.pop(get_robots_checker, None)

    @pytest.mark.xdist_group("uvicorn_drain")
    def test_uvicorn_subprocess_enters_drain_on_sigterm(self) -> None:
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[2]
        env = {
            **os.environ,
            "ENVIRONMENT": "development",
            "REDIS_URI": "",
            "OTEL_ENABLED": "false",
            "SHUTDOWN_PRE_STOP_DELAY": "0.1",
            "PYTHONUNBUFFERED": "1",
        }
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "--app-dir",
                str(repo_root / "src"),
                "downloader.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                "0",
                "--log-level",
                "info",
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        captured_lines: list[str] = []
        try:
            assert proc.stdout is not None
            ready = False
            deadline = time.monotonic() + 20.0
            import select

            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                rlist, _, _ = select.select([proc.stdout], [], [], 0.1)
                if rlist:
                    line = proc.stdout.readline()
                    if not line:
                        break
                    captured_lines.append(line)
                    if "Uvicorn running on" in line:
                        ready = True
                        break
            if not ready:
                proc.terminate()
                tail, _ = proc.communicate(timeout=5.0)
                pytest.fail(
                    f"Uvicorn subprocess did not report ready (rc={proc.returncode}):\n"
                    f"{''.join(captured_lines)}{tail}"
                )

            proc.send_signal(signal.SIGTERM)
            tail, _ = proc.communicate(timeout=15.0)
            output = "".join(captured_lines) + tail
            assert proc.returncode in (0, -signal.SIGTERM), output
            assert "Entering connection drain mode" in output
            assert "All services shut down successfully" in output
            assert "Finished server process" in output
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=5.0)


@pytest.mark.unit
async def test_teardown_isolation_catches_unlisted_exceptions_and_worker_metrics_enforces_auth() -> (
    None
):
    """`S-13` & `S-14`: `_run_teardown_step` isolates arbitrary `Exception` subclasses and worker `/metrics` enforces `DOWNLOADER_KEY`."""
    from downloader.config import AuthConfig
    from downloader.main import _run_teardown_step
    from downloader.worker_runtime import _start_worker_metrics_server

    ran_second_step = False

    async def _bad_step() -> None:
        raise KeyError("unexpected-shutdown-key-error")

    async def _good_step() -> None:
        nonlocal ran_second_step
        ran_second_step = True

    await _run_teardown_step("bad_step", _bad_step)
    await _run_teardown_step("good_step", _good_step)
    assert ran_second_step is True

    auth_cfg = Settings(auth=AuthConfig(api_key="worker-metrics-secret"))
    srv = await _start_worker_metrics_server(0, settings=auth_cfg)
    assert srv.sockets
    port = srv.sockets[0].getsockname()[1]
    try:
        # 1. Unauthenticated scrape -> 401 Unauthorized
        r1, w1 = await asyncio.open_connection("127.0.0.1", port)
        w1.write(b"GET /metrics HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        await w1.drain()
        unauth_resp = await r1.read(4096)
        w1.close()
        await w1.wait_closed()
        assert unauth_resp.startswith(b"HTTP/1.1 401 Unauthorized")

        # 2. Invalid Bearer token -> 401 Unauthorized
        r2, w2 = await asyncio.open_connection("127.0.0.1", port)
        w2.write(b"GET /metrics HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer wrong\r\n\r\n")
        await w2.drain()
        wrong_resp = await r2.read(4096)
        w2.close()
        await w2.wait_closed()
        assert wrong_resp.startswith(b"HTTP/1.1 401 Unauthorized")

        # 3. Valid Bearer token -> 200 OK with Prometheus exposition
        r3, w3 = await asyncio.open_connection("127.0.0.1", port)
        w3.write(
            b"GET /metrics HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Authorization: Bearer worker-metrics-secret\r\n\r\n"
        )
        await w3.drain()
        ok_resp = await r3.read(8192)
        w3.close()
        await w3.wait_closed()
        assert ok_resp.startswith(b"HTTP/1.1 200 OK")
        assert b"downloader_uptime_seconds" in ok_resp
    finally:
        srv.close()
        await srv.wait_closed()
