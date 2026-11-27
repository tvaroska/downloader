"""SSRF-safe outbound webhook delivery with HMAC-SHA256 signing, secret sealing, and retries."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import Settings, get_settings
from ..ssrf_transport import SSRFSafeHTTPTransport
from ..telemetry import get_tracer, inject_trace_headers
from ..validation import (
    EgressPolicy,
    SSRFProtectionError,
    URLValidationError,
    canonical_origin,
    format_origin_url,
    redact_url_for_log,
    validate_url_async,
)

logger = logging.getLogger(__name__)

WEBHOOK_SECRET_PREFIX = "enc:v1:"
_NONCE_BYTES = 16
_MAC_BYTES = 32
_PROCESS_FALLBACK_KEY = secrets.token_bytes(32)


def _derive_secret_keys(cfg: Settings) -> tuple[bytes, bytes]:
    """Derive Encrypt-then-MAC keys from dedicated `WEBHOOK_SECRET_KEY` (required when Redis/worker is set)."""
    dedicated_key = (cfg.webhook.secret_key or "").strip()
    if dedicated_key:
        material = dedicated_key.encode("utf-8")
    elif cfg.redis.redis_uri or cfg.worker.deployment_mode in {"api", "worker"}:
        raise ValueError(
            "WEBHOOK_SECRET_KEY is required to encrypt or decrypt stored webhook secrets "
            "when REDIS_URI or split-worker mode is configured"
        )
    else:
        fallback_key = (cfg.auth.api_key or "").strip()
        material = fallback_key.encode("utf-8") if fallback_key else _PROCESS_FALLBACK_KEY
    enc_key = hmac.new(material, b"downloader:webhook-secret:enc:v1", hashlib.sha256).digest()
    mac_key = hmac.new(material, b"downloader:webhook-secret:mac:v1", hashlib.sha256).digest()
    return enc_key, mac_key


def _hmac_keystream(enc_key: bytes, nonce: bytes, length: int) -> bytes:
    blocks: list[bytes] = []
    counter = 0
    produced = 0
    while produced < length:
        block = hmac.new(enc_key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        blocks.append(block)
        produced += len(block)
        counter += 1
    return b"".join(blocks)[:length]


def seal_webhook_secret(secret: str | None, settings: Settings | None = None) -> str | None:
    """Encrypt a webhook signing secret before persisting it to Redis (Encrypt-then-MAC)."""
    if not secret:
        return None
    if secret.startswith(WEBHOOK_SECRET_PREFIX):
        raise ValueError("webhook_secret must not start with reserved 'enc:v1:' prefix")
    cfg = settings or get_settings()
    enc_key, mac_key = _derive_secret_keys(cfg)
    plaintext = secret.encode("utf-8")
    nonce = secrets.token_bytes(_NONCE_BYTES)
    stream = _hmac_keystream(enc_key, nonce, len(plaintext))
    ciphertext = bytes(a ^ b for a, b in zip(plaintext, stream, strict=True))
    mac = hmac.new(
        mac_key,
        WEBHOOK_SECRET_PREFIX.encode("ascii") + nonce + ciphertext,
        hashlib.sha256,
    ).digest()
    token = base64.urlsafe_b64encode(nonce + mac + ciphertext).decode("ascii")
    return f"{WEBHOOK_SECRET_PREFIX}{token}"


def open_webhook_secret(sealed: str | None, settings: Settings | None = None) -> str | None:
    """Decrypt a sealed webhook signing secret; legacy unprefixed secrets pass through unchanged."""
    if not sealed:
        return None
    if not sealed.startswith(WEBHOOK_SECRET_PREFIX):
        return sealed
    cfg = settings or get_settings()
    enc_key, mac_key = _derive_secret_keys(cfg)
    raw = base64.urlsafe_b64decode(sealed.removeprefix(WEBHOOK_SECRET_PREFIX).encode("ascii"))
    if len(raw) < _NONCE_BYTES + _MAC_BYTES:
        raise ValueError("Corrupted encrypted webhook secret")
    nonce = raw[:_NONCE_BYTES]
    mac = raw[_NONCE_BYTES : _NONCE_BYTES + _MAC_BYTES]
    ciphertext = raw[_NONCE_BYTES + _MAC_BYTES :]
    expected_mac = hmac.new(
        mac_key,
        WEBHOOK_SECRET_PREFIX.encode("ascii") + nonce + ciphertext,
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(mac, expected_mac):
        raise ValueError("Webhook secret integrity check failed")
    stream = _hmac_keystream(enc_key, nonce, len(ciphertext))
    plaintext = bytes(a ^ b for a, b in zip(ciphertext, stream, strict=True))
    return plaintext.decode("utf-8")


def _format_webhook_log_target(url: str) -> str:
    """Return only scheme and host (plus non-default port) for safe webhook logging."""
    origin = canonical_origin(url)
    if origin is not None:
        return format_origin_url(*origin)
    return redact_url_for_log(url)


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


async def _post_with_retries(  # noqa: PLR0913
    client: httpx.AsyncClient,
    validated_url: str,
    event: str,
    delivery_id: str,
    body: bytes,
    headers: dict[str, str],
    cfg: Settings,
    span: Any,
) -> WebhookDeliveryResult:
    max_attempts = max(1, cfg.webhook.max_retries)
    last_status: int | None = None
    last_error: str | None = None
    safe_target = _format_webhook_log_target(validated_url)

    for attempt in range(1, max_attempts + 1):
        try:
            resp = await client.post(validated_url, content=body, headers=headers)
            last_status = resp.status_code
            span.set_attribute("http.status_code", resp.status_code)

            if 200 <= resp.status_code < 300:
                logger.info(
                    f"Webhook delivered: event={event} delivery={delivery_id} "
                    f"target={safe_target} status={resp.status_code} attempt={attempt}"
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
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                logger.warning(
                    f"Webhook rejected ({resp.status_code}) for {safe_target} "
                    f"delivery={delivery_id}; not retrying"
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
            logger.warning(
                f"Webhook blocked during connect ({safe_target}) delivery={delivery_id}: "
                f"{type(exc).__name__}"
            )
            return WebhookDeliveryResult(
                delivered=False,
                webhook_url=validated_url,
                event=event,
                delivery_id=delivery_id,
                attempts=attempt,
                error=str(exc),
            )
        except (httpx.HTTPError, OSError, RuntimeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                f"Webhook attempt {attempt}/{max_attempts} failed for {safe_target} "
                f"delivery={delivery_id}: {type(exc).__name__}"
            )

        if attempt < max_attempts and cfg.webhook.backoff_base > 0:
            await asyncio.sleep(cfg.webhook.backoff_base * (2 ** (attempt - 1)))

    return WebhookDeliveryResult(
        delivered=False,
        webhook_url=validated_url,
        event=event,
        delivery_id=delivery_id,
        attempts=max_attempts,
        status_code=last_status,
        error=last_error,
    )


async def deliver_webhook(  # noqa: PLR0913
    webhook_url: str,
    event: str,
    payload: dict[str, Any],
    *,
    secret: str | None = None,
    settings: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> WebhookDeliveryResult:
    """Deliver a JSON webhook payload safely with SSRF validation and bounded retries."""
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
            logger.warning(
                f"Blocked unsafe webhook URL ({_format_webhook_log_target(webhook_url)}) "
                f"delivery={delivery_id}: {type(exc).__name__}"
            )
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

        active_transport = transport or SSRFSafeHTTPTransport(egress_policy=EgressPolicy(cfg))
        owns_transport = transport is None
        try:
            async with httpx.AsyncClient(
                transport=active_transport,
                timeout=cfg.webhook.timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                return await _post_with_retries(
                    client, validated_url, event, delivery_id, body, headers, cfg, span
                )
        finally:
            if owns_transport and hasattr(active_transport, "aclose"):
                try:
                    await active_transport.aclose()
                except (httpx.HTTPError, OSError, RuntimeError) as exc:
                    logger.debug(f"Error closing webhook transport: {exc}")
