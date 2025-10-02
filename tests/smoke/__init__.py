"""Smoke tests - fast sanity checks for every commit (<3s total).

These tests validate core functionality without external dependencies:
- Server starts and responds
- Configuration loads correctly
- Authentication logic works
- SSRF and URL validation
- Rate limiting configuration
- Basic content conversion (mocked)
"""
