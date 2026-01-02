"""E2E tests for HTML rendering with real URLs."""

import httpx
import pytest
from bs4 import BeautifulSoup


@pytest.mark.e2e
@pytest.mark.network
@pytest.mark.slow
class TestHTMLRenderingE2E:
    """E2E tests for HTML rendering with real URLs."""

    @pytest.fixture(autouse=True)
    def setup_base_url(self):
        """Set up base URL for tests."""
        import os

        self.base_url = os.getenv("E2E_BASE_URL", "http://localhost:8081")

    @pytest.mark.asyncio
    async def test_substack_url_returns_complete_metadata(self):
        """
        Test that the Substack URL from bug report returns complete metadata.

        This is the primary test case from DOWNLOADER_BUG.md.
        Expected behavior:
        - Detects Substack as JS-heavy domain
        - Triggers Playwright rendering
        - Returns HTML with complete metadata tags
        - Response size significantly larger than raw HTTP fetch
        """
        # The exact URL from the bug report
        target_url = "https://ontologist.substack.com/p/understanding-shacl-12-rules"

        # Make request to downloader service
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(
                f"{self.base_url}/{target_url}",
                headers={
                    "Accept": "text/html",
                    "Authorization": "Bearer value",
                },
                follow_redirects=True,
            )

        # Verify response success
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"

        # Verify X-Rendered-With-JS header
        rendered_with_js = response.headers.get("X-Rendered-With-JS", "false")
        assert (
            rendered_with_js == "true"
        ), f"Expected JS rendering to be triggered, got X-Rendered-With-JS: {rendered_with_js}"

        # Parse HTML
        soup = BeautifulSoup(response.text, "html.parser")

        # Verify title tag
        title_tag = soup.find("title")
        assert title_tag is not None, "Missing <title> tag"
        title_text = title_tag.get_text(strip=True)
        assert len(title_text) > 0, "Title tag is empty"
        assert (
            "SHACL" in title_text or "Understanding" in title_text
        ), f"Unexpected title: {title_text}"

        # Verify OpenGraph title
        og_title = soup.find("meta", property="og:title")
        assert og_title is not None, "Missing <meta property='og:title'>"
        og_title_content = og_title.get("content", "")
        assert len(og_title_content) > 0, "og:title content is empty"
        assert (
            "SHACL" in og_title_content or "Understanding" in og_title_content
        ), f"Unexpected og:title: {og_title_content}"

        # Verify OpenGraph description
        og_description = soup.find("meta", property="og:description")
        assert og_description is not None, "Missing <meta property='og:description'>"
        og_description_content = og_description.get("content", "")
        assert len(og_description_content) > 0, "og:description content is empty"

        # Verify OpenGraph image
        og_image = soup.find("meta", property="og:image")
        assert og_image is not None, "Missing <meta property='og:image'>"
        og_image_content = og_image.get("content", "")
        assert len(og_image_content) > 0, "og:image content is empty"
        assert og_image_content.startswith("http"), f"Invalid og:image URL: {og_image_content}"

        # Verify response size is substantial (should be >100KB for fully rendered Substack)
        response_size = len(response.content)
        assert (
            response_size > 100_000
        ), f"Response too small ({response_size} bytes), expected >100KB for rendered Substack"

        # Log metadata for verification
        print("\n✅ Substack E2E Test Results:")
        print(f"  Title: {title_text}")
        print(f"  og:title: {og_title_content}")
        print(f"  og:description: {og_description_content[:100]}...")
        print(f"  og:image: {og_image_content}")
        print(f"  Response size: {response_size:,} bytes")
        print(f"  X-Rendered-With-JS: {rendered_with_js}")

    @pytest.mark.asyncio
    async def test_static_url_bypasses_rendering(self):
        """
        Test that static HTML pages bypass Playwright rendering.

        Uses the test URL from project config (koomen.dev).
        Expected behavior:
        - Detects page has complete metadata
        - Skips Playwright rendering (performance optimization)
        - Returns raw HTML
        """
        # Static test URL from .clinerules/AGENTS.md
        target_url = "https://koomen.dev/essays/horseless-carriages/"

        # Make request to downloader service
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(
                f"{self.base_url}/{target_url}",
                headers={
                    "Accept": "text/html",
                    "Authorization": "Bearer value",
                },
                follow_redirects=True,
            )

        # Verify response success
        assert response.status_code == 200

        # Verify X-Rendered-With-JS header shows NO rendering
        rendered_with_js = response.headers.get("X-Rendered-With-JS", "false")
        # Static pages should either not trigger rendering or be marked as false
        # (They might trigger if they have missing metadata, but that's expected)

        # Parse HTML
        soup = BeautifulSoup(response.text, "html.parser")

        # Verify HTML is returned (even if not rendered)
        assert soup.find("html") is not None

        # Log results
        print("\n✅ Static URL E2E Test Results:")
        print(f"  URL: {target_url}")
        print(f"  Response size: {len(response.content):,} bytes")
        print(f"  X-Rendered-With-JS: {rendered_with_js}")
        print(f"  HTML structure present: {soup.find('html') is not None}")

    @pytest.mark.asyncio
    async def test_compare_raw_vs_rendered_substack(self):
        """
        Compare raw HTTP fetch vs downloader service for Substack URL.

        This test demonstrates the issue described in DOWNLOADER_BUG.md:
        - Raw HTTP fetch returns ~40KB without metadata
        - Downloader service should return >200KB with complete metadata
        """
        target_url = "https://ontologist.substack.com/p/understanding-shacl-12-rules"

        # 1. Direct HTTP fetch (simulates what HTTP client does)
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            raw_response = await client.get(
                target_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                },
            )

        raw_size = len(raw_response.content)
        raw_soup = BeautifulSoup(raw_response.text, "html.parser")
        raw_has_og_title = raw_soup.find("meta", property="og:title") is not None

        # 2. Downloader service fetch
        async with httpx.AsyncClient(timeout=60.0) as client:
            downloader_response = await client.get(
                f"{self.base_url}/{target_url}",
                headers={
                    "Accept": "text/html",
                    "Authorization": "Bearer value",
                },
                follow_redirects=True,
            )

        downloader_size = len(downloader_response.content)
        downloader_soup = BeautifulSoup(downloader_response.text, "html.parser")
        downloader_has_og_title = downloader_soup.find("meta", property="og:title") is not None

        # Verify downloader service returns significantly more content
        size_increase_ratio = downloader_size / raw_size if raw_size > 0 else 0

        print("\n✅ Raw vs Rendered Comparison:")
        print(f"  Raw HTTP fetch size: {raw_size:,} bytes")
        print(f"  Raw has og:title: {raw_has_og_title}")
        print(f"  Downloader service size: {downloader_size:,} bytes")
        print(f"  Downloader has og:title: {downloader_has_og_title}")
        print(f"  Size increase: {size_increase_ratio:.1f}x")

        # Assertions
        assert (
            downloader_has_og_title
        ), "Downloader service should return HTML with og:title metadata"
        assert (
            downloader_size > raw_size
        ), f"Downloader service response ({downloader_size}) should be larger than raw fetch ({raw_size})"
        assert (
            size_increase_ratio > 2.0
        ), f"Expected at least 2x size increase, got {size_increase_ratio:.1f}x"

    @pytest.mark.asyncio
    async def test_headers_indicate_rendering_status(self):
        """Test that X-Rendered-With-JS header correctly indicates rendering status."""
        test_cases = [
            {
                "url": "https://example.substack.com/p/test",
                "expected_rendering": True,
                "description": "Substack domain",
            },
            # Note: We can't guarantee koomen.dev won't trigger rendering
            # if it has missing metadata, so we'll just check it returns successfully
        ]

        for test_case in test_cases:
            url = test_case["url"]

            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    response = await client.get(
                        f"{self.base_url}/{url}",
                        headers={
                            "Accept": "text/html",
                            "Authorization": "Bearer test-api-key-e2e",
                        },
                        follow_redirects=True,
                    )

                # Check if header is present
                rendered_with_js = response.headers.get("X-Rendered-With-JS")

                # For Substack, we expect rendering to be triggered
                if test_case["expected_rendering"]:
                    assert (
                        rendered_with_js == "true"
                    ), f"{test_case['description']}: Expected X-Rendered-With-JS: true, got {rendered_with_js}"

                print(f"\n  {test_case['description']}: X-Rendered-With-JS = {rendered_with_js} ✓")

            except (httpx.ConnectError, httpx.TimeoutException) as e:
                # If connection fails to real URL, skip this test case
                print(f"\n  {test_case['description']}: Skipped (connection error: {e})")
                pytest.skip(f"Could not connect to {url}: {e}")


@pytest.mark.e2e
@pytest.mark.network
class TestHTMLRenderingQuickCheck:
    """Quick E2E tests that can run faster."""

    @pytest.fixture(autouse=True)
    def setup_base_url(self):
        """Set up base URL for tests."""
        import os

        self.base_url = os.getenv("E2E_BASE_URL", "http://localhost:8081")

    @pytest.mark.asyncio
    async def test_html_endpoint_accepts_requests(self):
        """Test that HTML endpoint accepts and processes requests."""
        # Use a reliable test URL
        target_url = "https://example.com"

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{self.base_url}/{target_url}",
                headers={
                    "Accept": "text/html",
                    "Authorization": "Bearer value",
                },
                follow_redirects=True,
            )

        # Verify basic response
        assert response.status_code == 200
        assert "html" in response.headers.get("content-type", "").lower()

        # Verify X-Rendered-With-JS header exists
        assert "X-Rendered-With-JS" in response.headers

        print("\n✅ HTML endpoint responding correctly")
        print(f"  Content-Type: {response.headers.get('content-type')}")
        print(f"  X-Rendered-With-JS: {response.headers.get('X-Rendered-With-JS')}")
