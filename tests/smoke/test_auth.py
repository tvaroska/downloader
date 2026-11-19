"""Tests for authentication functionality."""

import ast
import os
import secrets
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import ValidationError

from downloader.auth import (
    get_api_key,
    get_auth_status,
    is_auth_enabled,
    verify_api_key,
)
from downloader.config import AuthConfig, Settings, reload_settings


@pytest.mark.smoke
class TestAuthFunctions:
    """Test authentication utility functions."""

    @pytest.mark.parametrize(
        "key, expected",
        [("", False), ("   ", False), (None, False), ("test-key-123", True)],
    )
    def test_is_auth_enabled(self, key, expected):
        """Test is_auth_enabled with different keys."""
        settings = Settings(auth=AuthConfig(api_key=key))
        assert is_auth_enabled(settings) is expected

    @pytest.mark.parametrize(
        "env_key, provided_key, expected",
        [
            (None, "any-key", True),
            ("secret-key", "secret-key", True),
            ("secret-key", "wrong-key", False),
        ],
    )
    def test_verify_api_key(self, env_key, provided_key, expected):
        """Test API key verification."""
        settings = Settings(auth=AuthConfig(api_key=env_key))
        assert verify_api_key(provided_key, settings) is expected

    def test_get_auth_status_disabled(self):
        """Test auth status when authentication is disabled."""
        settings = Settings(auth=AuthConfig(api_key=None))
        status = get_auth_status(settings)
        assert status["auth_enabled"] is False
        assert status["auth_methods"] is None

    def test_get_auth_status_enabled(self):
        """Test auth status when authentication is enabled."""
        settings = Settings(auth=AuthConfig(api_key="test-key"))
        status = get_auth_status(settings)
        assert status["auth_enabled"] is True
        assert isinstance(status["auth_methods"], list)
        assert len(status["auth_methods"]) == 2

    def test_verify_api_key_uses_compare_digest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pin constant-time `secrets.compare_digest` usage in `verify_api_key` (`R54`)."""
        observed_pairs: list[tuple[bytes, bytes]] = []
        real_compare = secrets.compare_digest

        def _spy_compare_digest(a: bytes, b: bytes) -> bool:
            observed_pairs.append((a, b))
            return real_compare(a, b)

        monkeypatch.setattr(secrets, "compare_digest", _spy_compare_digest)
        settings = Settings(auth=AuthConfig(api_key="const-time-secret"))
        assert verify_api_key("const-time-secret", settings) is True
        assert verify_api_key("wrong-candidate", settings) is False

        assert observed_pairs == [
            (b"const-time-secret", b"const-time-secret"),
            (b"wrong-candidate", b"const-time-secret"),
        ]

        auth_path = Path(__file__).resolve().parents[2] / "src" / "downloader" / "auth.py"
        calls = [
            node.func.attr
            for node in ast.walk(ast.parse(auth_path.read_text(encoding="utf-8")))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        ]
        assert "compare_digest" in calls


@pytest.mark.smoke
class TestGetApiKey:
    """Test get_api_key function."""

    @pytest.fixture
    def mock_request(self):
        """Mock FastAPI request object."""
        request = AsyncMock(spec=Request)
        request.headers = {}
        request.query_params = {}
        request.app = None
        return request

    @pytest.mark.asyncio
    async def test_no_auth_required(self, mock_request):
        """Test when no authentication is required."""
        with patch.dict(os.environ, {}, clear=True):
            reload_settings()
            try:
                result = await get_api_key(mock_request, None)
                assert result is None
            finally:
                reload_settings()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "auth_header, expected_result",
        [
            ({"Authorization": "Bearer test-key"}, "test-key"),
            ({"x-api-key": "test-key"}, "test-key"),
        ],
    )
    async def test_get_api_key_valid(self, mock_request, auth_header, expected_result):
        """Test valid API key from different sources."""
        try:
            with patch.dict(os.environ, {"DOWNLOADER_KEY": "test-key"}, clear=True):
                reload_settings()
                mock_request.headers = auth_header
                credentials = (
                    HTTPAuthorizationCredentials(scheme="Bearer", credentials="test-key")
                    if "Authorization" in auth_header
                    else None
                )
                result = await get_api_key(mock_request, credentials)
                assert result == expected_result
        finally:
            reload_settings()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "auth_header, credentials, error_detail",
        [
            (
                {},
                None,
                "authentication_required",
            ),
            (
                {"Authorization": "Bearer wrong-key"},
                HTTPAuthorizationCredentials(scheme="Bearer", credentials="wrong-key"),
                "authentication_failed",
            ),
            (
                {"x-api-key": "wrong-key"},
                None,
                "authentication_failed",
            ),
        ],
    )
    async def test_get_api_key_invalid(self, mock_request, auth_header, credentials, error_detail):
        """Test invalid or missing API key scenarios."""
        try:
            with patch.dict(os.environ, {"DOWNLOADER_KEY": "test-key"}, clear=True):
                reload_settings()
                mock_request.headers = auth_header
                with pytest.raises(HTTPException) as exc_info:
                    await get_api_key(mock_request, credentials)

                assert exc_info.value.status_code == 401
                assert error_detail in str(exc_info.value.detail)
        finally:
            reload_settings()

    @pytest.mark.asyncio
    async def test_bearer_token_priority(self, mock_request):
        """Test Bearer token takes priority over other methods."""
        try:
            with patch.dict(os.environ, {"DOWNLOADER_KEY": "test-key"}, clear=True):
                reload_settings()
                credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="test-key")
                mock_request.headers = {"x-api-key": "wrong-key"}

                result = await get_api_key(mock_request, credentials)
                assert result == "test-key"
        finally:
            reload_settings()


@pytest.mark.smoke
@pytest.mark.parametrize("blank_key", [None, "", "   "])
def test_production_without_key_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, blank_key: str | None
) -> None:
    """Production refuses to start without a non-empty `DOWNLOADER_KEY` unless opted out (`R37`)."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("AUTH_ALLOW_UNAUTHENTICATED_PRODUCTION", raising=False)
    if blank_key is None:
        monkeypatch.delenv("DOWNLOADER_KEY", raising=False)
    else:
        monkeypatch.setenv("DOWNLOADER_KEY", blank_key)

    with pytest.raises(ValidationError, match="DOWNLOADER_KEY"):
        Settings()

    # Explicit opt-out allows unauthenticated production startup
    monkeypatch.setenv("AUTH_ALLOW_UNAUTHENTICATED_PRODUCTION", "true")
    opted_out = Settings()
    assert opted_out.environment == "production"
    assert is_auth_enabled(opted_out) is False

    # Non-empty key succeeds without opt-out
    monkeypatch.delenv("AUTH_ALLOW_UNAUTHENTICATED_PRODUCTION", raising=False)
    monkeypatch.setenv("DOWNLOADER_KEY", "prod-secret-key")
    prod_settings = Settings()
    assert prod_settings.environment == "production"
    assert is_auth_enabled(prod_settings) is True
