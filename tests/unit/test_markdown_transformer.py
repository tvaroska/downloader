"""Tests for HTML to Markdown transformer."""

import pytest

from src.downloader.transformers import html_to_markdown


@pytest.mark.unit
class TestMarkdownStructurePreservation:
    """Test that HTML structure is preserved in markdown conversion."""

    @pytest.mark.parametrize(
        "level,html_tag,expected_prefix",
        [
            (1, "h1", "# "),
            (2, "h2", "## "),
            (3, "h3", "### "),
            (4, "h4", "#### "),
            (5, "h5", "##### "),
            (6, "h6", "###### "),
        ],
    )
    def test_heading_conversion(self, level, html_tag, expected_prefix):
        """Test headings h1-h6 are converted to ATX style."""
        html = f"<html><body><{html_tag}>Test Heading</{html_tag}></body></html>"
        result = html_to_markdown(html)
        assert f"{expected_prefix}Test Heading" in result

    def test_unordered_list_conversion(self):
        """Test unordered lists are converted with bullets."""
        html = """
        <html><body>
            <ul>
                <li>First item</li>
                <li>Second item</li>
                <li>Third item</li>
            </ul>
        </body></html>
        """
        result = html_to_markdown(html)
        assert "* First item" in result
        assert "* Second item" in result
        assert "* Third item" in result

    def test_ordered_list_conversion(self):
        """Test ordered lists preserve numbering."""
        html = """
        <html><body>
            <ol>
                <li>First step</li>
                <li>Second step</li>
                <li>Third step</li>
            </ol>
        </body></html>
        """
        result = html_to_markdown(html)
        assert "1. First step" in result
        assert "2. Second step" in result
        assert "3. Third step" in result

    def test_link_conversion(self):
        """Test links are converted to markdown format."""
        html = '<html><body><p>Visit <a href="https://example.com">Example</a> for more.</p></body></html>'
        result = html_to_markdown(html)
        assert "[Example](https://example.com)" in result

    def test_code_block_with_language(self):
        """Test code blocks preserve language from class attribute."""
        html = """
        <html><body>
            <pre><code class="language-python">def hello():
    print("Hello")</code></pre>
        </body></html>
        """
        result = html_to_markdown(html)
        assert "```python" in result
        assert 'print("Hello")' in result
        assert "```" in result

    def test_code_block_without_language(self):
        """Test code blocks without language class use plain fence."""
        html = """
        <html><body>
            <pre><code>some code here</code></pre>
        </body></html>
        """
        result = html_to_markdown(html)
        assert "```\nsome code here\n```" in result

    def test_inline_code_conversion(self):
        """Test inline code is wrapped in backticks."""
        html = "<html><body><p>Use the <code>print()</code> function.</p></body></html>"
        result = html_to_markdown(html)
        assert "`print()`" in result

    def test_bold_italic_conversion(self):
        """Test bold and italic text conversion."""
        html = "<html><body><p><strong>Bold</strong> and <em>italic</em> text.</p></body></html>"
        result = html_to_markdown(html)
        assert "**Bold**" in result
        assert "*italic*" in result


@pytest.mark.unit
class TestContentExtraction:
    """Test main content extraction from different container elements."""

    def test_article_tag_extraction(self):
        """Test content is extracted from article tag."""
        html = """
        <html><body>
            <nav>Navigation here</nav>
            <article>
                <h1>Article Title</h1>
                <p>Article content.</p>
            </article>
            <footer>Footer here</footer>
        </body></html>
        """
        result = html_to_markdown(html)
        assert "# Article Title" in result
        assert "Article content" in result
        assert "Navigation" not in result
        assert "Footer" not in result

    def test_main_tag_extraction(self):
        """Test content is extracted from main tag."""
        html = """
        <html><body>
            <header>Header here</header>
            <main>
                <h1>Main Content</h1>
                <p>Main text.</p>
            </main>
        </body></html>
        """
        result = html_to_markdown(html)
        assert "# Main Content" in result
        assert "Main text" in result
        assert "Header" not in result

    def test_role_main_extraction(self):
        """Test content is extracted from element with role=main."""
        html = """
        <html><body>
            <aside>Sidebar</aside>
            <div role="main">
                <h1>Main Area</h1>
                <p>Content here.</p>
            </div>
        </body></html>
        """
        result = html_to_markdown(html)
        assert "# Main Area" in result
        assert "Content here" in result
        assert "Sidebar" not in result

    @pytest.mark.parametrize(
        "class_name",
        ["content", "post-content", "entry-content", "article-content"],
    )
    def test_class_content_extraction(self, class_name):
        """Test content is extracted from common content class selectors."""
        html = f"""
        <html><body>
            <nav>Menu</nav>
            <div class="{class_name}">
                <h1>Class Content</h1>
                <p>Extracted text.</p>
            </div>
        </body></html>
        """
        result = html_to_markdown(html)
        assert "# Class Content" in result
        assert "Extracted text" in result

    def test_fallback_to_body(self):
        """Test fallback to body when no main content container found."""
        html = """
        <html><body>
            <h1>Page Title</h1>
            <p>Some body content.</p>
        </body></html>
        """
        result = html_to_markdown(html)
        assert "# Page Title" in result
        assert "Some body content" in result


@pytest.mark.unit
class TestTagStripping:
    """Test that non-content tags are stripped."""

    def test_default_tag_stripping(self):
        """Test default tags are stripped: script, style, nav, header, footer, aside, menu, form."""
        html = """
        <html><body>
            <script>alert('xss')</script>
            <style>.red { color: red; }</style>
            <nav>Navigation</nav>
            <header>Site Header</header>
            <main>
                <h1>Content</h1>
                <p>Main text.</p>
            </main>
            <footer>Site Footer</footer>
            <aside>Sidebar</aside>
            <menu>Menu items</menu>
            <form>Form content</form>
        </body></html>
        """
        result = html_to_markdown(html)
        assert "# Content" in result
        assert "Main text" in result
        assert "alert" not in result
        assert ".red" not in result
        assert "Navigation" not in result
        assert "Site Header" not in result
        assert "Site Footer" not in result
        assert "Sidebar" not in result
        assert "Menu items" not in result
        assert "Form content" not in result

    def test_custom_tag_stripping(self):
        """Test additional tags can be stripped via strip_tags parameter."""
        html = """
        <html><body>
            <div class="advertisement">Buy now!</div>
            <main>
                <h1>Article</h1>
                <p>Real content.</p>
            </main>
        </body></html>
        """
        # Without custom stripping, advertisement div would appear
        result_default = html_to_markdown(html, extract_main_content=False)
        assert "Buy now" in result_default

        # With custom stripping of div tags
        result_stripped = html_to_markdown(html, strip_tags=["div"], extract_main_content=False)
        assert "Buy now" not in result_stripped


@pytest.mark.unit
class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_empty_html(self):
        """Test empty HTML returns empty string."""
        html = "<html><body></body></html>"
        result = html_to_markdown(html)
        assert result == ""

    def test_malformed_html(self):
        """Test malformed HTML is handled gracefully."""
        html = "<html><body><div>Unclosed tags<p>Malformed</html>"
        result = html_to_markdown(html)
        assert "Unclosed tags" in result
        assert "Malformed" in result

    def test_bytes_input(self):
        """Test bytes input is decoded correctly."""
        html = b"<html><body><h1>Bytes Input</h1><p>Works fine.</p></body></html>"
        result = html_to_markdown(html)
        assert "# Bytes Input" in result
        assert "Works fine" in result

    def test_bytes_input_with_unicode(self):
        """Test bytes input with unicode characters."""
        html = "<html><body><p>Hello 世界 🌍</p></body></html>".encode()
        result = html_to_markdown(html)
        assert "Hello 世界 🌍" in result

    def test_nested_lists(self):
        """Test nested list structures."""
        html = """
        <html><body>
            <ul>
                <li>Item 1
                    <ul>
                        <li>Nested A</li>
                        <li>Nested B</li>
                    </ul>
                </li>
                <li>Item 2</li>
            </ul>
        </body></html>
        """
        result = html_to_markdown(html)
        assert "Item 1" in result
        assert "Nested A" in result
        assert "Nested B" in result
        assert "Item 2" in result

    def test_whitespace_normalization(self):
        """Test multiple blank lines are normalized to single blank line."""
        html = """
        <html><body>
            <p>Paragraph one.</p>



            <p>Paragraph two.</p>
        </body></html>
        """
        result = html_to_markdown(html)
        # Should not have more than 2 consecutive newlines
        assert "\n\n\n" not in result
        assert "Paragraph one" in result
        assert "Paragraph two" in result

    def test_link_in_heading(self):
        """Test links within headings are preserved."""
        html = '<html><body><h2><a href="https://example.com">Linked Heading</a></h2></body></html>'
        result = html_to_markdown(html)
        assert "## [Linked Heading](https://example.com)" in result


@pytest.mark.unit
class TestParameters:
    """Test function parameters."""

    def test_setext_heading_style(self):
        """Test setext heading style produces underlined headings."""
        html = "<html><body><h1>Title</h1><h2>Subtitle</h2></body></html>"
        result = html_to_markdown(html, heading_style="setext")
        # Setext uses === for h1 and --- for h2
        assert "===" in result or "Title\n=" in result
        assert "---" in result or "Subtitle\n-" in result

    def test_custom_bullets(self):
        """Test custom bullet character."""
        html = """
        <html><body>
            <ul>
                <li>Item A</li>
                <li>Item B</li>
            </ul>
        </body></html>
        """
        result = html_to_markdown(html, bullets="-")
        assert "- Item A" in result
        assert "- Item B" in result

    def test_extract_main_content_false(self):
        """Test extract_main_content=False uses full document."""
        html = """
        <html><body>
            <div class="wrapper">
                <h1>Full Page</h1>
                <p>All content included.</p>
            </div>
        </body></html>
        """
        result = html_to_markdown(html, extract_main_content=False)
        assert "# Full Page" in result
        assert "All content included" in result
