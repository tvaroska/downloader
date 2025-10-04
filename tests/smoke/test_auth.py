"""Tests for authentication functionality."""

import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials

from src.downloader.auth import (
    get_api_key,
    get_auth_status,
    is_auth_enabled,
    verify_api_key,
)


@pytest.mark.smoke
class TestAuthFunctions:
    """Test authentication utility functions."""

    @pytest.mark.parametrize(
        "key, expected",
        [("", False), ("   ", False), (None, False), ("test-key-123", True)],
    )
    def test_is_auth_enabled(self, key, expected):
        """Test is_auth_enabled with different keys."""
        env = {"DOWNLOADER_KEY": key} if key is not None else {}
        with patch.dict(os.environ, env, clear=True):
            assert is_auth_enabled() is expected

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
        env = {"DOWNLOADER_KEY": env_key} if env_key is not None else {}
        with patch.dict(os.environ, env, clear=True):
            assert verify_api_key(provided_key) is expected

    def test_get_auth_status_disabled(self):
        """Test auth status when authentication is disabled."""
        with patch.dict(os.environ, {}, clear=True):
            status = get_auth_status()
            assert status["auth_enabled"] is False
            assert status["auth_methods"] is None

    def test_get_auth_status_enabled(self):
        """Test auth status when authentication is enabled."""
        with patch.dict(os.environ, {"DOWNLOADER_KEY": "test-key"}, clear=True):
            status = get_auth_status()
            assert status["auth_enabled"] is True
            assert isinstance(status["auth_methods"], list)
            assert len(status["auth_methods"]) == 2


@pytest.mark.smoke
class TestGetApiKey:
    """Test get_api_key function."""

    @pytest.fixture
    def mock_request(self):
        """Mock FastAPI request object."""
        request = AsyncMock(spec=Request)
        request.headers = {}
        request.query_params = {}
        return request

    @pytest.mark.asyncio
    async def test_no_auth_required(self, mock_request):
        """Test when no authentication is required."""
        with patch.dict(os.environ, {}, clear=True):
            result = await get_api_key(mock_request, None)
            assert result is None

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
        with patch.dict(os.environ, {"DOWNLOADER_KEY": "test-key"}, clear=True):
            mock_request.headers = auth_header
            credentials = (
                HTTPAuthorizationCredentials(scheme="Bearer", credentials="test-key")
                if "Authorization" in auth_header
                else None
            )
            result = await get_api_key(mock_request, credentials)
            assert result == expected_result

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
        with patch.dict(os.environ, {"DOWNLOADER_KEY": "test-key"}, clear=True):
            mock_request.headers = auth_header
            with pytest.raises(HTTPException) as exc_info:
                await get_api_key(mock_request, credentials)

            assert exc_info.value.status_code == 401
            assert error_detail in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_bearer_token_priority(self, mock_request):
        """Test Bearer token takes priority over other methods."""
        with patch.dict(os.environ, {"DOWNLOADER_KEY": "test-key"}, clear=True):
            credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="test-key")
            mock_request.headers = {"x-api-key": "wrong-key"}

            result = await get_api_key(mock_request, credentials)
            assert result == "test-key"
