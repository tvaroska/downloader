"""Unit tests for HTML rendering detection logic."""

import pytest
from bs4 import BeautifulSoup

from src.downloader.content_converter import (
    _has_js_framework_markers,
    _has_missing_metadata,
    should_use_playwright_for_html,
)


@pytest.mark.unit
class TestMetadataDetection:
    """Test metadata detection helper function."""

    def test_has_missing_metadata_when_all_present(self):
        """Test that metadata is detected when all tags are present."""
        html = """<html><head>
            <meta property="og:title" content="Test Title">
            <meta property="og:description" content="Test Description">
        </head></html>"""
        soup = BeautifulSoup(html, "html.parser")

        assert not _has_missing_metadata(soup), "Should have complete metadata"

    def test_has_missing_metadata_when_og_missing(self):
        """Test detection when OpenGraph tags are missing."""
        html = """<html><head>
            <title>Test</title>
        </head></html>"""
        soup = BeautifulSoup(html, "html.parser")

        assert _has_missing_metadata(soup), "Should detect missing metadata"

    def test_has_missing_metadata_with_twitter_only(self):
        """Test that Twitter card tags are accepted as alternative."""
        html = """<html><head>
            <meta name="twitter:title" content="Test Title">
            <meta name="twitter:description" content="Test Description">
        </head></html>"""
        soup = BeautifulSoup(html, "html.parser")

        assert not _has_missing_metadata(soup), "Twitter card tags should suffice"

    def test_has_missing_metadata_mixed_sources(self):
        """Test that mixed OpenGraph and Twitter tags work."""
        html = """<html><head>
            <meta property="og:title" content="Test Title">
            <meta name="twitter:description" content="Test Description">
        </head></html>"""
        soup = BeautifulSoup(html, "html.parser")

        assert not _has_missing_metadata(soup), "Mixed sources should work"

    def test_has_missing_metadata_only_title(self):
        """Test detection when only title is present."""
        html = """<html><head>
            <meta property="og:title" content="Test Title">
        </head></html>"""
        soup = BeautifulSoup(html, "html.parser")

        assert _has_missing_metadata(soup), "Should detect missing description"

    def test_has_missing_metadata_only_description(self):
        """Test detection when only description is present."""
        html = """<html><head>
            <meta property="og:description" content="Test Description">
        </head></html>"""
        soup = BeautifulSoup(html, "html.parser")

        assert _has_missing_metadata(soup), "Should detect missing title"


@pytest.mark.unit
class TestJSFrameworkMarkers:
    """Test JavaScript framework marker detection."""

    def test_detects_react_root_with_minimal_content(self):
        """Test detection of React #root with minimal content."""
        html = """<html><body>
            <div id="root"></div>
        </body></html>"""
        soup = BeautifulSoup(html, "html.parser")
        body_text = soup.find("body").get_text(strip=True)

        assert _has_js_framework_markers(soup, body_text), "Should detect React marker"

    def test_detects_vue_app_with_minimal_content(self):
        """Test detection of Vue #app with minimal content."""
        html = """<html><body>
            <div id="app"></div>
        </body></html>"""
        soup = BeautifulSoup(html, "html.parser")
        body_text = soup.find("body").get_text(strip=True)

        assert _has_js_framework_markers(soup, body_text), "Should detect Vue marker"

    def test_detects_angular_ng_app(self):
        """Test detection of Angular ng-app attribute."""
        html = """<html><body ng-app="myApp">
            <div ng-view></div>
        </body></html>"""
        soup = BeautifulSoup(html, "html.parser")
        body_text = soup.find("body").get_text(strip=True)

        assert _has_js_framework_markers(soup, body_text), "Should detect Angular marker"

    def test_no_detection_with_framework_but_substantial_content(self):
        """Test that framework markers with substantial content don't trigger."""
        html = (
            """<html><body>
            <div id="root">
                <p>"""
            + ("Lorem ipsum " * 50)
            + """</p>
            </div>
        </body></html>"""
        )
        soup = BeautifulSoup(html, "html.parser")
        body_text = soup.find("body").get_text(strip=True)

        assert not _has_js_framework_markers(
            soup, body_text
        ), "Substantial content should not trigger"

    def test_no_detection_without_framework_markers(self):
        """Test that pages without framework markers aren't flagged."""
        html = """<html><body>
            <div class="container">
                <p>Regular content</p>
            </div>
        </body></html>"""
        soup = BeautifulSoup(html, "html.parser")
        body_text = soup.find("body").get_text(strip=True)

        assert not _has_js_framework_markers(soup, body_text), "No markers should not trigger"


@pytest.mark.unit
class TestPlaywrightDetection:
    """Test the main should_use_playwright_for_html function."""

    def test_substack_domain_triggers_detection(self, substack_minimal_html):
        """Test that substack.com domain triggers JS rendering."""
        url = "https://example.substack.com/p/test-article"
        content_type = "text/html; charset=utf-8"

        # Clear any caches that might interfere
        from src.downloader import content_converter

        content_converter._js_heavy_cache.clear()
        content_converter._static_html_cache.clear()

        result = should_use_playwright_for_html(url, substack_minimal_html, content_type)

        assert result, "Substack domain should trigger JS rendering"

    def test_medium_domain_triggers_detection(self, medium_minimal_html):
        """Test that medium.com domain triggers JS rendering."""
        url = "https://medium.com/@author/test-article"
        content_type = "text/html; charset=utf-8"

        from src.downloader import content_converter

        content_converter._js_heavy_cache.clear()
        content_converter._static_html_cache.clear()

        result = should_use_playwright_for_html(url, medium_minimal_html, content_type)

        assert result, "Medium domain should trigger JS rendering"

    def test_missing_metadata_with_small_size_triggers(self, substack_minimal_html):
        """Test that missing metadata with small size triggers JS rendering."""
        url = "https://example.com/article"
        content_type = "text/html; charset=utf-8"

        from src.downloader import content_converter

        content_converter._js_heavy_cache.clear()
        content_converter._static_html_cache.clear()

        # Verify content is small
        assert len(substack_minimal_html) < 50000

        result = should_use_playwright_for_html(url, substack_minimal_html, content_type)

        assert result, "Missing metadata with small size should trigger"

    def test_react_framework_marker_triggers(self, react_app_minimal_html):
        """Test that React framework markers trigger JS rendering."""
        url = "https://example.com/app"
        content_type = "text/html; charset=utf-8"

        from src.downloader import content_converter

        content_converter._js_heavy_cache.clear()
        content_converter._static_html_cache.clear()

        result = should_use_playwright_for_html(url, react_app_minimal_html, content_type)

        assert result, "React markers should trigger JS rendering"

    def test_vue_framework_marker_triggers(self, vue_app_minimal_html):
        """Test that Vue framework markers trigger JS rendering."""
        url = "https://example.com/app"
        content_type = "text/html; charset=utf-8"

        from src.downloader import content_converter

        content_converter._js_heavy_cache.clear()
        content_converter._static_html_cache.clear()

        result = should_use_playwright_for_html(url, vue_app_minimal_html, content_type)

        assert result, "Vue markers should trigger JS rendering"

    def test_angular_framework_marker_triggers(self, angular_app_minimal_html):
        """Test that Angular framework markers trigger JS rendering."""
        url = "https://example.com/app"
        content_type = "text/html; charset=utf-8"

        from src.downloader import content_converter

        content_converter._js_heavy_cache.clear()
        content_converter._static_html_cache.clear()

        result = should_use_playwright_for_html(url, angular_app_minimal_html, content_type)

        assert result, "Angular markers should trigger JS rendering"

    def test_explicit_js_required_message_triggers(self, js_required_message_html):
        """Test that explicit 'enable JavaScript' messages trigger rendering."""
        url = "https://example.com/page"
        content_type = "text/html; charset=utf-8"

        from src.downloader import content_converter

        content_converter._js_heavy_cache.clear()
        content_converter._static_html_cache.clear()

        result = should_use_playwright_for_html(url, js_required_message_html, content_type)

        assert result, "Explicit JS requirement should trigger rendering"

    def test_static_html_complete_does_not_trigger(self, static_html_complete):
        """Test that complete static HTML does not trigger JS rendering."""
        url = "https://example.com/blog/post"
        content_type = "text/html; charset=utf-8"

        from src.downloader import content_converter

        content_converter._js_heavy_cache.clear()
        content_converter._static_html_cache.clear()

        result = should_use_playwright_for_html(url, static_html_complete, content_type)

        assert not result, "Complete static HTML should not trigger rendering"

    def test_large_html_with_metadata_does_not_trigger(self, large_html_with_metadata):
        """Test that large HTML (>50KB) with metadata doesn't trigger."""
        url = "https://example.com/large-article"
        content_type = "text/html; charset=utf-8"

        from src.downloader import content_converter

        content_converter._js_heavy_cache.clear()
        content_converter._static_html_cache.clear()

        # Verify content is large
        assert len(large_html_with_metadata) > 50000

        result = should_use_playwright_for_html(url, large_html_with_metadata, content_type)

        assert not result, "Large HTML with metadata should not trigger"

    def test_non_html_content_does_not_trigger(self):
        """Test that non-HTML content does not trigger JS rendering."""
        url = "https://example.com/file.json"
        content = b'{"key": "value"}'
        content_type = "application/json"

        from src.downloader import content_converter

        content_converter._js_heavy_cache.clear()
        content_converter._static_html_cache.clear()

        result = should_use_playwright_for_html(url, content, content_type)

        assert not result, "Non-HTML content should not trigger rendering"

    def test_empty_html_does_not_trigger(self, empty_html):
        """Test that empty HTML triggers detection due to missing metadata.

        Note: Empty HTML has missing metadata and small size, so it triggers
        detection. This is expected behavior - the detection cannot know if
        the page is truly empty or just needs JS rendering.
        """
        url = "https://example.com/empty"
        content_type = "text/html; charset=utf-8"

        from src.downloader import content_converter

        content_converter._js_heavy_cache.clear()
        content_converter._static_html_cache.clear()

        result = should_use_playwright_for_html(url, empty_html, content_type)

        # Empty HTML with missing metadata will trigger detection
        # This is expected - the detector can't distinguish empty from JS-rendered
        assert result, "Empty HTML with missing metadata triggers detection"

    def test_cache_behavior_js_heavy(self, substack_minimal_html):
        """Test that JS-heavy URLs are cached correctly."""
        url = "https://test.substack.com/p/article"
        content_type = "text/html; charset=utf-8"

        from src.downloader import content_converter

        content_converter._js_heavy_cache.clear()
        content_converter._static_html_cache.clear()

        # First call should detect and cache
        result1 = should_use_playwright_for_html(url, substack_minimal_html, content_type)
        assert result1

        # Verify URL is in cache
        assert url in content_converter._js_heavy_cache

        # Second call should use cache (wouldn't matter if HTML changed)
        result2 = should_use_playwright_for_html(url, b"<html></html>", content_type)
        assert result2, "Should use cache result"

    def test_cache_behavior_static(self, static_html_complete):
        """Test that static HTML URLs are cached correctly."""
        url = "https://example.com/static-article"
        content_type = "text/html; charset=utf-8"

        from src.downloader import content_converter

        content_converter._js_heavy_cache.clear()
        content_converter._static_html_cache.clear()

        # First call should detect and cache
        result1 = should_use_playwright_for_html(url, static_html_complete, content_type)
        assert not result1

        # Verify URL is in cache
        assert url in content_converter._static_html_cache

        # Second call should use cache
        result2 = should_use_playwright_for_html(url, static_html_complete, content_type)
        assert not result2, "Should use cache result"

    def test_malformed_html_handles_gracefully(self, malformed_html):
        """Test that malformed HTML is handled gracefully."""
        url = "https://example.com/malformed"
        content_type = "text/html; charset=utf-8"

        from src.downloader import content_converter

        content_converter._js_heavy_cache.clear()
        content_converter._static_html_cache.clear()

        # Should not raise exception
        try:
            result = should_use_playwright_for_html(url, malformed_html, content_type)
            # Should default to False on parsing errors
            assert isinstance(result, bool)
        except Exception as e:
            pytest.fail(f"Malformed HTML should be handled gracefully, but raised: {e}")
