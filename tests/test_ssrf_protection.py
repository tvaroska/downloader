"""Test SSRF protection implementation."""

import pytest

from src.downloader.config import Settings, SSRFConfig
from src.downloader.validation import (
    SSRFProtectionError,
    URLValidationError,
    validate_url,
)


class TestSSRFProtection:
    """Test comprehensive SSRF protection."""

    def test_block_localhost_ip(self):
        """Block loopback IP addresses."""
        settings = Settings(ssrf=SSRFConfig(resolve_dns=False))

        with pytest.raises(SSRFProtectionError, match="loopback"):
            validate_url("http://127.0.0.1", settings)

        with pytest.raises(SSRFProtectionError, match="loopback"):
            validate_url("http://127.0.0.2", settings)

    def test_block_localhost_ipv6(self):
        """Block IPv6 loopback."""
        settings = Settings(ssrf=SSRFConfig(resolve_dns=False))

        with pytest.raises(SSRFProtectionError, match="loopback"):
            validate_url("http://[::1]", settings)

    def test_block_private_ips_class_a(self):
        """Block private IP addresses in 10.0.0.0/8 range."""
        settings = Settings(ssrf=SSRFConfig(resolve_dns=False, block_private_ips=True))

        with pytest.raises(SSRFProtectionError, match="private IP"):
            validate_url("http://10.0.0.1", settings)

        with pytest.raises(SSRFProtectionError, match="private IP"):
            validate_url("http://10.255.255.255", settings)

    def test_block_private_ips_class_b(self):
        """Block private IP addresses in 172.16.0.0/12 range."""
        settings = Settings(ssrf=SSRFConfig(resolve_dns=False, block_private_ips=True))

        with pytest.raises(SSRFProtectionError, match="private IP"):
            validate_url("http://172.16.0.1", settings)

        with pytest.raises(SSRFProtectionError, match="private IP"):
            validate_url("http://172.31.255.255", settings)

    def test_block_private_ips_class_c(self):
        """Block private IP addresses in 192.168.0.0/16 range."""
        settings = Settings(ssrf=SSRFConfig(resolve_dns=False, block_private_ips=True))

        with pytest.raises(SSRFProtectionError, match="private IP"):
            validate_url("http://192.168.0.1", settings)

        with pytest.raises(SSRFProtectionError, match="private IP"):
            validate_url("http://192.168.255.255", settings)

    def test_block_cloud_metadata_ip(self):
        """Block cloud metadata endpoint IP."""
        settings = Settings(ssrf=SSRFConfig(resolve_dns=False, block_cloud_metadata=True))

        with pytest.raises(SSRFProtectionError, match="cloud metadata"):
            validate_url("http://169.254.169.254", settings)

        with pytest.raises(SSRFProtectionError, match="cloud metadata"):
            validate_url("http://169.254.169.254/latest/meta-data/", settings)

    def test_block_link_local(self):
        """Block link-local addresses (169.254.0.0/16)."""
        settings = Settings(ssrf=SSRFConfig(resolve_dns=False, block_cloud_metadata=True))

        # Any 169.254.x.x address should be blocked (link-local)
        with pytest.raises(SSRFProtectionError, match="link-local"):
            validate_url("http://169.254.1.1", settings)

        with pytest.raises(SSRFProtectionError, match="link-local"):
            validate_url("http://169.254.100.50", settings)

    def test_block_multicast(self):
        """Block multicast addresses."""
        settings = Settings(ssrf=SSRFConfig(resolve_dns=False))

        with pytest.raises(SSRFProtectionError, match="multicast"):
            validate_url("http://224.0.0.1", settings)

        with pytest.raises(SSRFProtectionError, match="multicast"):
            validate_url("http://239.255.255.255", settings)

    def test_block_reserved(self):
        """Block reserved IP addresses."""
        settings = Settings(ssrf=SSRFConfig(resolve_dns=False))

        with pytest.raises(SSRFProtectionError, match="reserved"):
            validate_url("http://240.0.0.1", settings)

    def test_block_unspecified(self):
        """Block unspecified addresses (0.0.0.0)."""
        settings = Settings(ssrf=SSRFConfig(resolve_dns=False))

        with pytest.raises(SSRFProtectionError, match="unspecified"):
            validate_url("http://0.0.0.0", settings)

    def test_allow_private_ips_when_disabled(self):
        """Allow private IPs when block_private_ips=False."""
        settings = Settings(ssrf=SSRFConfig(resolve_dns=False, block_private_ips=False))

        # Should not raise (private IPs allowed)
        result = validate_url("http://192.168.1.1", settings)
        assert result == "http://192.168.1.1"

        result = validate_url("http://10.0.0.1", settings)
        assert result == "http://10.0.0.1"

    def test_allow_public_ips(self):
        """Allow legitimate public IP addresses."""
        settings = Settings(ssrf=SSRFConfig(resolve_dns=False, block_private_ips=True))

        # Google Public DNS
        result = validate_url("http://8.8.8.8", settings)
        assert result == "http://8.8.8.8"

        # Cloudflare DNS
        result = validate_url("http://1.1.1.1", settings)
        assert result == "http://1.1.1.1"

    def test_block_localhost_hostname_without_dns(self):
        """Block localhost hostname when DNS resolution is disabled."""
        settings = Settings(ssrf=SSRFConfig(resolve_dns=False))

        with pytest.raises(SSRFProtectionError, match="restricted address"):
            validate_url("http://localhost", settings)

        with pytest.raises(SSRFProtectionError, match="restricted address"):
            validate_url("http://localhost.localdomain", settings)

    def test_block_localhost_hostname_with_dns(self):
        """Block localhost hostname with DNS resolution."""
        settings = Settings(ssrf=SSRFConfig(resolve_dns=True))

        # localhost resolves to 127.0.0.1, which is loopback
        with pytest.raises(SSRFProtectionError, match="loopback"):
            validate_url("http://localhost", settings)

    def test_allow_public_domains_without_dns(self):
        """Allow public domains when DNS resolution is disabled."""
        settings = Settings(ssrf=SSRFConfig(resolve_dns=False, block_private_ips=True))

        # Should not raise (hostname-based check passes)
        result = validate_url("http://example.com", settings)
        assert result == "http://example.com"

        result = validate_url("http://google.com", settings)
        assert result == "http://google.com"

    def test_dns_resolution_for_public_domain(self):
        """Test DNS resolution for legitimate public domains."""
        settings = Settings(ssrf=SSRFConfig(resolve_dns=True, block_private_ips=True))

        # example.com should resolve to public IPs
        result = validate_url("http://example.com", settings)
        assert result == "http://example.com"

    def test_invalid_hostname_format(self):
        """Test that invalid hostname formats are rejected."""
        settings = Settings(ssrf=SSRFConfig(resolve_dns=False))

        # Special characters not allowed in hostnames
        with pytest.raises(URLValidationError, match="Invalid hostname format"):
            validate_url("http://host$name.com", settings)

        with pytest.raises(URLValidationError, match="Invalid hostname format"):
            validate_url("http://host name.com", settings)

    def test_url_scheme_validation(self):
        """Test that only http/https schemes are allowed."""
        settings = Settings(ssrf=SSRFConfig(resolve_dns=False))

        with pytest.raises(URLValidationError, match="file://"):
            validate_url("file:///etc/passwd", settings)

        with pytest.raises(URLValidationError, match="http or https"):
            validate_url("ftp://example.com", settings)

    def test_auto_add_http_scheme(self):
        """Test that http:// is auto-added if missing."""
        settings = Settings(ssrf=SSRFConfig(resolve_dns=False))

        result = validate_url("example.com", settings)
        assert result == "http://example.com"

        result = validate_url("8.8.8.8", settings)
        assert result == "http://8.8.8.8"

    def test_dns_resolution_failure(self):
        """Test handling of DNS resolution failures."""
        settings = Settings(ssrf=SSRFConfig(resolve_dns=True))

        # Non-existent domain should fail DNS resolution
        with pytest.raises(SSRFProtectionError, match="Cannot resolve hostname"):
            validate_url(
                "http://this-domain-definitely-does-not-exist-12345.com",
                settings,
            )

    def test_ipv6_private_addresses(self):
        """Test blocking of IPv6 private addresses."""
        settings = Settings(ssrf=SSRFConfig(resolve_dns=False, block_private_ips=True))

        # fd00::/8 is private IPv6 range
        with pytest.raises(SSRFProtectionError, match="private IP"):
            validate_url("http://[fd00::1]", settings)

    def test_ipv6_link_local(self):
        """Test blocking of IPv6 link-local addresses."""
        settings = Settings(ssrf=SSRFConfig(resolve_dns=False))

        # fe80::/10 is link-local IPv6 range
        with pytest.raises(SSRFProtectionError, match="link-local"):
            validate_url("http://[fe80::1]", settings)

    def test_empty_url(self):
        """Test that empty URLs are rejected."""
        settings = Settings(ssrf=SSRFConfig(resolve_dns=False))

        with pytest.raises(URLValidationError, match="non-empty string"):
            validate_url("", settings)

        with pytest.raises(URLValidationError, match="non-empty string"):
            validate_url(None, settings)

    def test_url_with_port(self):
        """Test that URLs with ports work correctly."""
        settings = Settings(ssrf=SSRFConfig(resolve_dns=False, block_private_ips=True))

        # Public IP with port should work
        result = validate_url("http://8.8.8.8:8080", settings)
        assert result == "http://8.8.8.8:8080"

        # Private IP with port should be blocked
        with pytest.raises(SSRFProtectionError):
            validate_url("http://192.168.1.1:8080", settings)

    def test_url_with_path_and_query(self):
        """Test that URLs with paths and query strings work correctly."""
        settings = Settings(ssrf=SSRFConfig(resolve_dns=False, block_private_ips=True))

        # Public IP with path/query
        result = validate_url("http://8.8.8.8/path?query=value", settings)
        assert result == "http://8.8.8.8/path?query=value"

        # Private IP with path/query should still be blocked
        with pytest.raises(SSRFProtectionError):
            validate_url("http://192.168.1.1/admin?token=secret", settings)

    def test_default_settings(self):
        """Test that validation works with default settings."""
        # Should use get_settings() internally
        result = validate_url("http://example.com")
        assert result == "http://example.com"

        # Loopback should still be blocked with defaults
        with pytest.raises(SSRFProtectionError):
            validate_url("http://127.0.0.1")
