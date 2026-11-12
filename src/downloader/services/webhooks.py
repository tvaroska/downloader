"""SSRF-safe outbound webhook delivery with HMAC-SHA256 signing and retries."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import Settings, get_settings
from ..ssrf_transport import SSRFSafeHTTPTransport
from ..telemetry import get_tracer, inject_trace_headers
from ..validation import SSRFProtectionError, URLValidationError, validate_url_async

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WebhookDeliveryResult:
    """Outcome summary for an outbound webhook delivery."""

    delivered: bool
    webhook_url: str
    event: str
    delivery_id: str
    attempts: int
    status_code: int | None = None
    error: str | None = None


def sign_webhook_payload(payload_bytes: bytes, secret: str) -> str:
    """Return GitHub-style HMAC-SHA256 signature header value for payload bytes."""
    digest = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


async def deliver_webhook(
    webhook_url: str,
    event: str,
    payload: dict[str, Any],
    *,
    secret: str | None = None,
    settings: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> WebhookDeliveryResult:
    """Deliver a JSON webhook payload safely with SSRF validation and bounded retries.

    Never follows HTTP redirects so target endpoints cannot redirect into private networks.
    Never raises on delivery failure after returning a structured WebhookDeliveryResult.
    """
    cfg = settings or get_settings()
    delivery_id = str(uuid.uuid4())

    if not cfg.webhook.enabled:
        logger.debug(f"Webhook delivery skipped (disabled): event={event}")
        return WebhookDeliveryResult(
            delivered=False,
            webhook_url=webhook_url,
            event=event,
            delivery_id=delivery_id,
            attempts=0,
            error="Webhook delivery disabled by configuration",
        )

    tracer = get_tracer(__name__)
    with tracer.start_as_current_span("webhook.deliver") as span:
        span.set_attribute("webhook.event", event)
        span.set_attribute("webhook.delivery_id", delivery_id)

        try:
            validated_url = await validate_url_async(webhook_url, cfg)
        except (URLValidationError, SSRFProtectionError) as exc:
            logger.warning(f"Blocked unsafe webhook URL ({webhook_url}): {exc}")
            span.set_attribute("webhook.blocked", True)
            return WebhookDeliveryResult(
                delivered=False,
                webhook_url=webhook_url,
                event=event,
                delivery_id=delivery_id,
                attempts=0,
                error=str(exc),
            )

        body = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
        headers = inject_trace_headers(
            {
                "Content-Type": "application/json",
                "User-Agent": cfg.crawling.user_agent,
                "X-Downloader-Event": event,
                "X-Downloader-Delivery": delivery_id,
            }
        )
        if secret:
            headers["X-Downloader-Signature-256"] = sign_webhook_payload(body, secret)

        active_transport = transport or SSRFSafeHTTPTransport(trust_env=False)
        owns_transport = transport is None
        max_attempts = max(1, cfg.webhook.max_retries)
        last_status: int | None = None
        last_error: str | None = None

        try:
            async with httpx.AsyncClient(
                transport=active_transport,
                timeout=cfg.webhook.timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                for attempt in range(1, max_attempts + 1):
                    try:
                        resp = await client.post(validated_url, content=body, headers=headers)
                        last_status = resp.status_code
                        span.set_attribute("http.status_code", resp.status_code)

                        if 200 <= resp.status_code < 300:
                            logger.info(
                                f"Webhook delivered: event={event} delivery={delivery_id} "
                                f"status={resp.status_code} attempt={attempt}"
                            )
                            return WebhookDeliveryResult(
                                delivered=True,
                                webhook_url=validated_url,
                                event=event,
                                delivery_id=delivery_id,
                                attempts=attempt,
                                status_code=resp.status_code,
                            )

                        last_error = f"HTTP {resp.status_code}"
                        # Do not retry deterministic 4xx client errors other than 429 rate limiting.
                        if 400 <= resp.status_code < 500 and resp.status_code != 429:
                            logger.warning(
                                f"Webhook rejected ({resp.status_code}) for {validated_url}; not retrying"
                            )
                            return WebhookDeliveryResult(
                                delivered=False,
                                webhook_url=validated_url,
                                event=event,
                                delivery_id=delivery_id,
                                attempts=attempt,
                                status_code=resp.status_code,
                                error=last_error,
                            )
                    except (URLValidationError, SSRFProtectionError) as exc:
                        logger.warning(f"Webhook blocked during connect ({validated_url}): {exc}")
                        return WebhookDeliveryResult(
                            delivered=False,
                            webhook_url=validated_url,
                            event=event,
                            delivery_id=delivery_id,
                            attempts=attempt,
                            error=str(exc),
                        )
                    except Exception as exc:
                        last_error = f"{type(exc).__name__}: {exc}"
                        logger.warning(
                            f"Webhook attempt {attempt}/{max_attempts} failed for {validated_url}: {last_error}"
                        )

                    if attempt < max_attempts and cfg.webhook.backoff_base > 0:
                        await asyncio.sleep(cfg.webhook.backoff_base * (2 ** (attempt - 1)))

        finally:
            if owns_transport and hasattr(active_transport, "aclose"):
                try:
                    await active_transport.aclose()
                except Exception:
                    pass

        return WebhookDeliveryResult(
            delivered=False,
            webhook_url=validated_url,
            event=event,
            delivery_id=delivery_id,
            attempts=max_attempts,
            status_code=last_status,
            error=last_error,
        )
