"""Tests for authentication functionality."""

import os
import pytest
from unittest.mock import patch, AsyncMock
from fastapi import HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials

from downloader.auth import (
    is_auth_enabled,
    verify_api_key,
    get_api_key,
    get_auth_status,
    APIKeyError
)


class TestAuthFunctions:
    """Test authentication utility functions."""
    
    def test_is_auth_enabled_no_key(self):
        """Test auth disabled when no API key is set."""
        with patch.dict(os.environ, {}, clear=True):
            assert is_auth_enabled() is False
    
    def test_is_auth_enabled_empty_key(self):
        """Test auth disabled when API key is empty."""
        with patch.dict(os.environ, {"DOWNLOADER_KEY": ""}, clear=True):
            assert is_auth_enabled() is False
    
    def test_is_auth_enabled_whitespace_key(self):
        """Test auth disabled when API key is only whitespace."""
        with patch.dict(os.environ, {"DOWNLOADER_KEY": "   "}, clear=True):
            assert is_auth_enabled() is False
    
    def test_is_auth_enabled_valid_key(self):
        """Test auth enabled when valid API key is set."""
        with patch.dict(os.environ, {"DOWNLOADER_KEY": "test-key-123"}, clear=True):
            assert is_auth_enabled() is True
    
    def test_verify_api_key_no_env_key(self):
        """Test API key verification when no environment key is set."""
        with patch.dict(os.environ, {}, clear=True):
            assert verify_api_key("any-key") is True
    
    def test_verify_api_key_correct(self):
        """Test API key verification with correct key."""
        with patch.dict(os.environ, {"DOWNLOADER_KEY": "secret-key"}, clear=True):
            assert verify_api_key("secret-key") is True
    
    def test_verify_api_key_incorrect(self):
        """Test API key verification with incorrect key."""
        with patch.dict(os.environ, {"DOWNLOADER_KEY": "secret-key"}, clear=True):
            assert verify_api_key("wrong-key") is False
    
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
    async def test_bearer_token_valid(self, mock_request):
        """Test valid Bearer token authentication."""
        with patch.dict(os.environ, {"DOWNLOADER_KEY": "test-key"}, clear=True):
            credentials = HTTPAuthorizationCredentials(
                scheme="Bearer",
                credentials="test-key"
            )
            result = await get_api_key(mock_request, credentials)
            assert result == "test-key"
    
    @pytest.mark.asyncio
    async def test_bearer_token_invalid(self, mock_request):
        """Test invalid Bearer token authentication."""
        with patch.dict(os.environ, {"DOWNLOADER_KEY": "test-key"}, clear=True):
            credentials = HTTPAuthorizationCredentials(
                scheme="Bearer",
                credentials="wrong-key"
            )
            with pytest.raises(HTTPException) as exc_info:
                await get_api_key(mock_request, credentials)
            
            assert exc_info.value.status_code == 401
            assert "authentication_failed" in str(exc_info.value.detail)
    
    @pytest.mark.asyncio
    async def test_x_api_key_header_valid(self, mock_request):
        """Test valid X-API-Key header authentication."""
        with patch.dict(os.environ, {"DOWNLOADER_KEY": "test-key"}, clear=True):
            mock_request.headers = {"x-api-key": "test-key"}
            result = await get_api_key(mock_request, None)
            assert result == "test-key"
    
    @pytest.mark.asyncio
    async def test_x_api_key_header_invalid(self, mock_request):
        """Test invalid X-API-Key header authentication."""
        with patch.dict(os.environ, {"DOWNLOADER_KEY": "test-key"}, clear=True):
            mock_request.headers = {"x-api-key": "wrong-key"}
            with pytest.raises(HTTPException) as exc_info:
                await get_api_key(mock_request, None)
            
            assert exc_info.value.status_code == 401
            assert "authentication_failed" in str(exc_info.value.detail)
    
    
    @pytest.mark.asyncio
    async def test_no_api_key_provided(self, mock_request):
        """Test when API key is required but not provided."""
        with patch.dict(os.environ, {"DOWNLOADER_KEY": "test-key"}, clear=True):
            with pytest.raises(HTTPException) as exc_info:
                await get_api_key(mock_request, None)
            
            assert exc_info.value.status_code == 401
            assert "authentication_required" in str(exc_info.value.detail)
    
    @pytest.mark.asyncio
    async def test_bearer_token_priority(self, mock_request):
        """Test Bearer token takes priority over other methods."""
        with patch.dict(os.environ, {"DOWNLOADER_KEY": "test-key"}, clear=True):
            # Set up multiple auth methods
            credentials = HTTPAuthorizationCredentials(
                scheme="Bearer",
                credentials="test-key"
            )
            mock_request.headers = {"x-api-key": "wrong-key"}
            
            # Bearer token should be used and succeed
            result = await get_api_key(mock_request, credentials)
            assert result == "test-key"