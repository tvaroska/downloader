"""Unit and API tests for SSRF-safe HMAC-SHA256 webhooks and secret sealing."""

from __future__ import annotations

import asyncio
import io
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from downloader.config import Settings
from downloader.dependencies import (
    get_domain_throttle,
    get_http_client,
    get_job_manager_dependency,
    get_robots_checker,
    get_scheduler_dependency,
)
from downloader.job_manager import JobManager
from downloader.main import app
from downloader.models.responses import BatchRequest, BatchURLRequest
from downloader.scheduler.executor import ScheduledJobExecutor
from downloader.services.webhooks import (
    deliver_webhook,
    open_webhook_secret,
    seal_webhook_secret,
    sign_webhook_payload,
)


def _make_tiny_png() -> bytes:
    img = Image.new("RGB", (32, 16), color=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class TestWebhookDeliveryAndIntegration:
    def test_hmac_signature_format(self) -> None:
        sig = sign_webhook_payload(b'{"ok":true}', "top-secret")
        assert sig.startswith("sha256=")
        assert len(sig) == len("sha256=") + 64
        assert sig == sign_webhook_payload(b'{"ok":true}', "top-secret")

    def test_signature_known_answer(self) -> None:
        # RFC 4231 Test Case 2 known-answer HMAC-SHA256 vector
        sig = sign_webhook_payload(b"what do ya want for nothing?", "Jefe")
        assert sig == "sha256=5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"

    def test_signature_depends_on_secret(self) -> None:
        payload = b'{"event":"batch.completed"}'
        sig_a = sign_webhook_payload(payload, "secret-alpha")
        sig_b = sign_webhook_payload(payload, "secret-beta")
        sig_empty = sign_webhook_payload(payload, "")
        assert sig_a != sig_b
        assert sig_a != sig_empty

    async def test_redirect_is_not_followed(self) -> None:
        cfg = Settings()
        cfg.webhook.max_retries = 1
        cfg.webhook.backoff_base = 0.0
        seen_urls: list[str] = []

        def _redirect_handler(request: httpx.Request) -> httpx.Response:
            seen_urls.append(str(request.url))
            return httpx.Response(
                302,
                headers={"Location": "https://evil.example.com/steal"},
                request=request,
            )

        with patch(
            "downloader.services.webhooks.validate_url_async",
            new=AsyncMock(return_value="https://hooks.example.com/cb"),
        ):
            res = await deliver_webhook(
                "https://hooks.example.com/cb",
                "batch.completed",
                {"job_id": "1"},
                settings=cfg,
                transport=httpx.MockTransport(_redirect_handler),
            )
        assert res.delivered is False
        assert res.status_code == 302
        assert seen_urls == ["https://hooks.example.com/cb"]

    async def test_429_is_retried(self) -> None:
        cfg = Settings()
        cfg.webhook.max_retries = 3
        cfg.webhook.backoff_base = 0.0
        attempts = 0

        def _rate_limit_handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(429, text="rate limited", request=request)
            return httpx.Response(200, text="ok", request=request)

        with patch(
            "downloader.services.webhooks.validate_url_async",
            new=AsyncMock(return_value="https://hooks.example.com/cb"),
        ):
            res = await deliver_webhook(
                "https://hooks.example.com/cb",
                "batch.completed",
                {"job_id": "1"},
                settings=cfg,
                transport=httpx.MockTransport(_rate_limit_handler),
            )
        assert res.delivered is True
        assert res.attempts == 2
        assert attempts == 2

    async def test_deliver_webhook_signs_retries_and_blocks_ssrf(self) -> None:
        cfg = Settings()
        cfg.webhook.max_retries = 3
        cfg.webhook.backoff_base = 0.0

        # SSRF blocked pre-flight -> 0 attempts
        blocked = await deliver_webhook(
            "http://127.0.0.1:8080/internal",
            "batch.completed",
            {"job_id": "1"},
            settings=cfg,
        )
        assert blocked.delivered is False
        assert blocked.attempts == 0

        # Retries on 500 then succeeds on attempt 2 with valid signature
        attempts: list[httpx.Request] = []

        def _retry_handler(request: httpx.Request) -> httpx.Response:
            attempts.append(request)
            if len(attempts) == 1:
                return httpx.Response(500, text="transient")
            return httpx.Response(202, text="accepted")

        transport = httpx.MockTransport(_retry_handler)
        with patch(
            "downloader.services.webhooks.validate_url_async",
            new=AsyncMock(return_value="https://hooks.example.com/cb"),
        ):
            res = await deliver_webhook(
                "https://hooks.example.com/cb",
                "batch.completed",
                {"job_id": "abc"},
                secret="hook-secret",  # pragma: allowlist secret
                settings=cfg,
                transport=transport,
            )
        assert res.delivered is True
        assert res.attempts == 2
        assert res.status_code == 202
        sent = attempts[-1]
        assert sent.headers["x-downloader-event"] == "batch.completed"
        assert sent.headers["x-downloader-signature-256"] == sign_webhook_payload(
            sent.content, "hook-secret"
        )

        # Deterministic 400 does NOT retry
        calls_400 = 0

        def _bad_handler(_: httpx.Request) -> httpx.Response:
            nonlocal calls_400
            calls_400 += 1
            return httpx.Response(400, text="bad payload")

        with patch(
            "downloader.services.webhooks.validate_url_async",
            new=AsyncMock(return_value="https://hooks.example.com/cb"),
        ):
            res_400 = await deliver_webhook(
                "https://hooks.example.com/cb",
                "batch.completed",
                {"job_id": "abc"},
                settings=cfg,
                transport=httpx.MockTransport(_bad_handler),
            )
        assert res_400.delivered is False
        assert res_400.attempts == 1
        assert calls_400 == 1

    async def test_batch_job_manager_fires_webhook_after_results_persisted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events: list[str] = []
        jm = JobManager("redis://localhost:6379")

        async def _update_status(job_id: str, status, **kwargs) -> bool:
            events.append(f"status:{status.value}")
            return True

        async def _complete_job(job_id: str, results, summary, **kwargs) -> bool:
            events.append("complete_job")
            return True

        monkeypatch.setattr(jm, "update_job_status", _update_status)
        monkeypatch.setattr(jm, "complete_job", _complete_job)

        async def _fake_processor(job_id: str, req: BatchRequest):
            events.append("process")
            return [{"url": "https://example.com", "success": True}], {"total_requests": 1}

        async def _fake_webhook(webhook_url, event, payload, **kwargs):
            events.append(f"webhook:{event}")

        req = BatchRequest(
            urls=[BatchURLRequest(url="https://example.com")],
            webhook_url="https://hooks.example.com/batch",
            webhook_secret="sec",  # pragma: allowlist secret
        )
        with patch("downloader.services.batch_processor.deliver_webhook", new=_fake_webhook):
            task = await jm.start_background_job("job-1", _fake_processor, req)
            await task

        assert events == [
            "status:running",
            "process",
            "complete_job",
            "webhook:batch.completed",
        ]

    def test_webhook_secret_seal_open_roundtrip_and_tamper_rejection(self) -> None:
        """Webhook secrets encrypt to `enc:v1:...` blobs and reject tampering (`R113`)."""
        cfg = Settings()
        cfg.webhook.secret_key = "unit-test-webhook-master-key"  # pragma: allowlist secret
        sealed = seal_webhook_secret("hook-secret-value", cfg)
        assert sealed is not None
        assert sealed.startswith("enc:v1:")
        assert "hook-secret-value" not in sealed
        assert open_webhook_secret(sealed, cfg) == "hook-secret-value"
        # Legacy cleartext value passes through cleanly for backward compatibility
        assert open_webhook_secret("legacy-plain", cfg) == "legacy-plain"
        # Tampered ciphertext fails MAC verification
        tampered = sealed[:-2] + ("AA" if not sealed.endswith("AA") else "BB")
        with pytest.raises(ValueError, match="integrity|MAC|decrypt|invalid|corrupt"):
            open_webhook_secret(tampered, cfg)

    async def test_scheduled_executor_screenshot_and_webhook(self) -> None:
        mock_http = MagicMock()
        mock_storage = MagicMock()
        mock_storage.store_execution = AsyncMock()
        executor = ScheduledJobExecutor(
            http_client=mock_http,
            storage=mock_storage,
            pdf_semaphore=asyncio.Semaphore(2),
        )
        with (
            patch(
                "downloader.scheduler.executor.validate_url_async",
                new=AsyncMock(return_value="https://example.com"),
            ),
            patch(
                "downloader.pdf_generator.generate_screenshot_from_url",
                new=AsyncMock(return_value=b"\x89PNG\r\n\x1a\nSCHED"),
            ),
            patch(
                "downloader.scheduler.executor.deliver_webhook",
                new=AsyncMock(),
            ) as mock_hook,
        ):
            execution = await executor.execute(
                "sched-1",
                "https://example.com",
                "screenshot",
                None,
                webhook_url="https://hooks.example.com/sched",
                webhook_secret="shh",  # pragma: allowlist secret
            )
        assert execution.success is True
        assert execution.content_size == len(b"\x89PNG\r\n\x1a\nSCHED")
        mock_hook.assert_awaited_once()
        assert mock_hook.await_args is not None
        assert mock_hook.await_args.args[1] == "schedule.completed"
        assert mock_hook.await_args.kwargs["secret"] == "shh"  # pragma: allowlist secret

    def test_schedule_api_validates_webhook_and_hides_secret(self) -> None:
        mock_scheduler = MagicMock()
        fake_job = MagicMock()
        fake_job.id = "sched-abc"
        fake_job.name = "Morning"
        fake_job.next_run_time = None
        fake_job.kwargs = {
            "url": "https://example.com",
            "format": "screenshot",
            "webhook_url": "https://hooks.example.com/ok",
            "webhook_secret": "never-leak-this",  # pragma: allowlist secret
            "created_at": "2026-11-12T00:00:00+00:00",
        }
        fake_job.trigger = "0 9 * * *"
        mock_scheduler.scheduler.add_job = MagicMock(return_value=fake_job)
        mock_scheduler.get_jobs = MagicMock(return_value=[fake_job])
        app.dependency_overrides[get_scheduler_dependency] = lambda: mock_scheduler

        try:
            with TestClient(app) as client:
                # Private loopback webhook rejected at creation time
                bad = client.post(
                    "/schedules",
                    json={
                        "name": "Bad",
                        "url": "https://example.com",
                        "cron_expression": "0 9 * * *",
                        "webhook_url": "http://127.0.0.1/hook",
                    },
                )
                assert bad.status_code == 400

                with patch(
                    "downloader.routes.schedules.validate_url_async",
                    new=AsyncMock(
                        side_effect=["https://example.com", "https://hooks.example.com/ok"]
                    ),
                ):
                    ok = client.post(
                        "/schedules",
                        json={
                            "name": "Morning",
                            "url": "https://example.com",
                            "cron_expression": "0 9 * * *",
                            "format": "screenshot",
                            "webhook_url": "https://hooks.example.com/ok",
                            "webhook_secret": "never-leak-this",  # pragma: allowlist secret
                        },
                    )
                assert ok.status_code == 200
                body = ok.json()
                assert body["webhook_url"] == "https://hooks.example.com/ok"
                assert "webhook_secret" not in body
                assert "never-leak-this" not in json.dumps(body)
                # Stored scheduler kwargs never contain cleartext webhook_secret (`R113`).
                add_kwargs = mock_scheduler.scheduler.add_job.call_args.kwargs["kwargs"]
                assert "webhook_secret" not in add_kwargs
                assert add_kwargs["webhook_secret_enc"].startswith("enc:v1:")
                assert "never-leak-this" not in json.dumps(add_kwargs)

                listed = client.get("/schedules")
                assert listed.status_code == 200
                assert "never-leak-this" not in listed.text
        finally:
            app.dependency_overrides.pop(get_robots_checker, None)
            app.dependency_overrides.pop(get_domain_throttle, None)
            app.dependency_overrides.pop(get_http_client, None)

    def test_batch_endpoint_rejects_ssrf_webhook_url(self) -> None:
        mock_jm = MagicMock()
        app.dependency_overrides[get_job_manager_dependency] = lambda: mock_jm
        try:
            with TestClient(app) as client:
                resp = client.post(
                    "/batch",
                    json={
                        "urls": [{"url": "https://example.com"}],
                        "webhook_url": "http://169.254.169.254/latest/meta-data/",
                    },
                )
                assert resp.status_code == 400
                assert resp.json()["detail"]["error_type"] == "validation_error"
        finally:
            app.dependency_overrides.pop(get_robots_checker, None)
            app.dependency_overrides.pop(get_domain_throttle, None)
            app.dependency_overrides.pop(get_http_client, None)
            app.dependency_overrides.pop(get_job_manager_dependency, None)


@pytest.mark.unit
async def test_webhook_logs_redact_url_credentials(caplog: pytest.LogCaptureFixture) -> None:
    """Webhook logs include only scheme, host, and delivery ID — never credentials or query tokens."""
    cfg = Settings()
    cfg.webhook.max_retries = 1
    cfg.webhook.backoff_base = 0.0

    with caplog.at_level("WARNING", logger="downloader.services.webhooks"):
        blocked = await deliver_webhook(
            "http://admin:supersecret@127.0.0.1:8080/hook?token=tok_blocked_999",
            "batch.completed",
            {"job_id": "1"},
            settings=cfg,
        )
        assert blocked.delivered is False

        def _fail_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, text="bad", request=request)

        with patch(
            "downloader.services.webhooks.validate_url_async",
            new=AsyncMock(
                return_value="https://user:topsecretpass@hooks.example.com/cb?token=tok_live_888"
            ),
        ):
            failed = await deliver_webhook(
                "https://user:topsecretpass@hooks.example.com/cb?token=tok_live_888",
                "batch.completed",
                {"job_id": "2"},
                settings=cfg,
                transport=httpx.MockTransport(_fail_handler),
            )
        assert failed.delivered is False

    logs = caplog.text
    assert "supersecret" not in logs
    assert "topsecretpass" not in logs
    assert "tok_blocked_999" not in logs
    assert "tok_live_888" not in logs
    assert "hooks.example.com" in logs
    assert blocked.delivery_id in logs
    assert failed.delivery_id in logs


@pytest.mark.unit
async def test_webhook_secret_key_rotation_dedicated_redis_key_and_enc_prefix_rejection() -> None:
    """`S-10`: Require `WEBHOOK_SECRET_KEY` when Redis is set, reject `enc:v1:` input, and record failed execution on rotation."""
    from pydantic import ValidationError

    from downloader.models.schedule import ExecutionStatus, ScheduleCreate
    from downloader.routes.schedules import scheduled_download_job, set_executor

    # 1. Reject caller-supplied `enc:v1:` prefix in models, seal_webhook_secret, and routes (`400`)
    with pytest.raises(ValidationError, match="enc:v1:"):
        BatchRequest(
            urls=[BatchURLRequest(url="https://example.com")],
            webhook_secret="enc:v1:attacker-supplied",  # pragma: allowlist secret
        )
    with pytest.raises(ValidationError, match="enc:v1:"):
        ScheduleCreate(
            name="BadPrefix",
            url="https://example.com",
            cron_expression="0 9 * * *",
            webhook_secret="enc:v1:attacker-supplied",  # pragma: allowlist secret
        )

    cfg_local = Settings(_env_file=None)
    cfg_local.webhook.secret_key = "dedicated-webhook-key"  # pragma: allowlist secret
    with pytest.raises(ValueError, match="enc:v1:"):
        seal_webhook_secret("enc:v1:already-prefixed", cfg_local)

    with TestClient(app) as client:
        bad_batch = client.post(
            "/batch",
            json={
                "urls": [{"url": "https://example.com"}],
                "webhook_secret": "enc:v1:smuggled",  # pragma: allowlist secret
            },
        )
        assert bad_batch.status_code == 400
        assert bad_batch.json()["detail"]["error_type"] == "validation_error"

    # 2. When REDIS_URI is set, require dedicated WEBHOOK_SECRET_KEY so DOWNLOADER_KEY rotation never breaks schedules
    cfg_redis_no_key = Settings(_env_file=None)
    cfg_redis_no_key.redis.redis_uri = "redis://localhost:6379/0"
    cfg_redis_no_key.auth.api_key = "api-key-v1"  # pragma: allowlist secret
    cfg_redis_no_key.webhook.secret_key = None
    with pytest.raises(ValueError, match="WEBHOOK_SECRET_KEY"):
        seal_webhook_secret("hook-secret", cfg_redis_no_key)

    cfg_redis_v1 = Settings(_env_file=None)
    cfg_redis_v1.redis.redis_uri = "redis://localhost:6379/0"
    cfg_redis_v1.auth.api_key = "api-key-v1"  # pragma: allowlist secret
    cfg_redis_v1.webhook.secret_key = "dedicated-webhook-key-stable"  # pragma: allowlist secret
    sealed = seal_webhook_secret("hook-secret", cfg_redis_v1)
    assert sealed is not None

    cfg_redis_v2 = Settings(_env_file=None)
    cfg_redis_v2.redis.redis_uri = "redis://localhost:6379/0"
    cfg_redis_v2.auth.api_key = "api-key-v2-rotated"  # pragma: allowlist secret
    cfg_redis_v2.webhook.secret_key = "dedicated-webhook-key-stable"  # pragma: allowlist secret
    assert open_webhook_secret(sealed, cfg_redis_v2) == "hook-secret"

    # 3. If WEBHOOK_SECRET_KEY itself rotates without re-sealing, scheduled execution records FAILED in storage
    cfg_rotated_wh = Settings(_env_file=None)
    cfg_rotated_wh.redis.redis_uri = "redis://localhost:6379/0"
    cfg_rotated_wh.webhook.secret_key = "different-webhook-key"  # pragma: allowlist secret
    mock_storage = MagicMock()
    mock_storage.redis = None
    mock_storage.redis_client = None
    mock_storage.store_execution = AsyncMock()
    executor = ScheduledJobExecutor(
        http_client=MagicMock(),
        storage=mock_storage,
        settings=cfg_rotated_wh,
    )
    set_executor(executor)
    try:
        await scheduled_download_job(
            "sched-rot",
            "https://example.com",
            "text",
            None,
            webhook_url="https://hooks.example.com/cb",
            webhook_secret_enc=sealed,
        )
    finally:
        set_executor(None)

    mock_storage.store_execution.assert_awaited_once()
    stored_exec = mock_storage.store_execution.await_args.args[0]
    assert stored_exec.schedule_id == "sched-rot"
    assert stored_exec.status == ExecutionStatus.FAILED
    assert stored_exec.success is False
    assert "Webhook secret" in (stored_exec.error_message or "")
