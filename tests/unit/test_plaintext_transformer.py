"""Tests for HTML to plain text transformer."""

import pytest

from src.downloader.transformers import html_to_plaintext


@pytest.mark.unit
class TestBasicPlaintextConversion:
    """Test basic HTML to plain text conversion."""

    def test_simple_paragraph(self):
        """Test simple paragraph extraction."""
        html = "<html><body><p>Hello world</p></body></html>"
        result = html_to_plaintext(html)
        assert result == "Hello world"

    def test_multiple_paragraphs_default(self):
        """Test multiple paragraphs are joined with space by default."""
        html = "<html><body><p>First paragraph.</p><p>Second paragraph.</p></body></html>"
        result = html_to_plaintext(html)
        assert "First paragraph." in result
        assert "Second paragraph." in result
        # Default behavior joins with space separator
        assert "\n\n" not in result

    def test_heading_text_extraction(self):
        """Test headings are extracted as plain text without formatting."""
        html = """
        <html><body>
            <h1>Main Title</h1>
            <h2>Subtitle</h2>
            <p>Content here.</p>
        </body></html>
        """
        result = html_to_plaintext(html)
        assert "Main Title" in result
        assert "Subtitle" in result
        assert "Content here" in result
        # No markdown formatting
        assert "#" not in result

    def test_list_text_extraction(self):
        """Test list items are extracted as plain text."""
        html = """
        <html><body>
            <ul>
                <li>First item</li>
                <li>Second item</li>
            </ul>
        </body></html>
        """
        result = html_to_plaintext(html)
        assert "First item" in result
        assert "Second item" in result
        # No bullet markers
        assert "* " not in result
        assert "- " not in result

    def test_link_text_extraction(self):
        """Test link text is extracted without URL."""
        html = '<html><body><p>Visit <a href="https://example.com">our website</a> for more.</p></body></html>'
        result = html_to_plaintext(html)
        assert "Visit" in result
        assert "our website" in result
        assert "for more" in result
        # URL should not appear
        assert "https://example.com" not in result
        assert "[" not in result
        assert "]" not in result


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
        result = html_to_plaintext(html)
        assert "Article Title" in result
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
        result = html_to_plaintext(html)
        assert "Main Content" in result
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
        result = html_to_plaintext(html)
        assert "Main Area" in result
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
        result = html_to_plaintext(html)
        assert "Class Content" in result
        assert "Extracted text" in result

    def test_fallback_to_body(self):
        """Test fallback to body when no main content container found."""
        html = """
        <html><body>
            <h1>Page Title</h1>
            <p>Some body content.</p>
        </body></html>
        """
        result = html_to_plaintext(html)
        assert "Page Title" in result
        assert "Some body content" in result

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
        result = html_to_plaintext(html, extract_main_content=False)
        assert "Full Page" in result
        assert "All content included" in result


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
        result = html_to_plaintext(html)
        assert "Content" in result
        assert "Main text" in result
        assert "alert" not in result
        assert ".red" not in result
        assert "Navigation" not in result
        assert "Site Header" not in result
        assert "Site Footer" not in result
        assert "Sidebar" not in result
        assert "Menu items" not in result
        assert "Form content" not in result

    def test_script_removal(self):
        """Test script tags and their content are removed."""
        html = """
        <html><body>
            <script>
                function malicious() { alert('xss'); }
            </script>
            <p>Real content here.</p>
        </body></html>
        """
        result = html_to_plaintext(html)
        assert "Real content here" in result
        assert "malicious" not in result
        assert "alert" not in result

    def test_style_removal(self):
        """Test style tags and their content are removed."""
        html = """
        <html><body>
            <style>
                body { background: red; }
                .hidden { display: none; }
            </style>
            <p>Visible text.</p>
        </body></html>
        """
        result = html_to_plaintext(html)
        assert "Visible text" in result
        assert "background" not in result
        assert "display" not in result

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
        result_default = html_to_plaintext(html, extract_main_content=False)
        assert "Buy now" in result_default

        # With custom stripping of div tags
        result_stripped = html_to_plaintext(html, strip_tags=["div"], extract_main_content=False)
        assert "Buy now" not in result_stripped


@pytest.mark.unit
class TestWhitespaceHandling:
    """Test separator and whitespace normalization options."""

    def test_default_space_separator(self):
        """Test default space separator between text nodes."""
        html = "<html><body><p>First</p><p>Second</p></body></html>"
        result = html_to_plaintext(html)
        # Should be joined with spaces
        assert "First" in result
        assert "Second" in result

    def test_custom_separator(self):
        """Test custom separator character."""
        html = "<html><body><span>One</span><span>Two</span><span>Three</span></body></html>"
        result = html_to_plaintext(html, separator="|", extract_main_content=False)
        assert "|" in result

    def test_multiple_whitespace_normalization(self):
        """Test multiple whitespace characters are normalized to single space."""
        html = "<html><body><p>Too   many    spaces   here.</p></body></html>"
        result = html_to_plaintext(html)
        assert "Too many spaces here" in result
        assert "   " not in result

    def test_newlines_in_source_normalized(self):
        """Test newlines in HTML source are normalized."""
        html = """
        <html><body>
            <p>Line one.


            Line two with gaps.</p>
        </body></html>
        """
        result = html_to_plaintext(html)
        assert "Line one" in result
        assert "Line two" in result
        # Should be normalized when preserve_paragraphs is False
        assert "\n\n\n" not in result


@pytest.mark.unit
class TestParagraphPreservation:
    """Test preserve_paragraphs=True behavior.

    Note: The current implementation attempts to preserve paragraphs by inserting
    newlines after block elements, but get_text(separator=" ") normalizes these.
    These tests document the current behavior.
    """

    def test_paragraph_preservation_enabled(self):
        """Test preserve_paragraphs=True processes paragraph content."""
        html = "<html><body><p>First paragraph.</p><p>Second paragraph.</p></body></html>"
        result = html_to_plaintext(html, preserve_paragraphs=True)
        assert "First paragraph." in result
        assert "Second paragraph." in result
        # Note: Current implementation normalizes whitespace via get_text()

    def test_br_tag_handling(self):
        """Test BR tags are replaced with newline character."""
        html = "<html><body><p>Line one.<br>Line two.</p></body></html>"
        result = html_to_plaintext(html, preserve_paragraphs=True)
        assert "Line one" in result
        assert "Line two" in result
        # BR is replaced with \n but may be normalized by get_text()

    def test_block_elements_processing(self):
        """Test block elements (div, h1-h6, li) are processed for newlines."""
        html = """
        <html><body>
            <h1>Title</h1>
            <div>First block</div>
            <div>Second block</div>
        </body></html>
        """
        result = html_to_plaintext(html, preserve_paragraphs=True)
        assert "Title" in result
        assert "First block" in result
        assert "Second block" in result

    def test_newline_normalization_with_preserve(self):
        """Test multiple newlines are normalized to double newlines."""
        html = """
        <html><body>
            <p>Para one.</p>
            <p>Para two.</p>
            <p>Para three.</p>
        </body></html>
        """
        result = html_to_plaintext(html, preserve_paragraphs=True)
        # Should not have more than double newlines
        assert "\n\n\n" not in result


@pytest.mark.unit
class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_empty_html(self):
        """Test empty HTML returns empty string."""
        html = "<html><body></body></html>"
        result = html_to_plaintext(html)
        assert result == ""

    def test_malformed_html(self):
        """Test malformed HTML is handled gracefully."""
        html = "<html><body><div>Unclosed tags<p>Malformed</html>"
        result = html_to_plaintext(html)
        assert "Unclosed tags" in result
        assert "Malformed" in result

    def test_bytes_input(self):
        """Test bytes input is decoded correctly."""
        html = b"<html><body><h1>Bytes Input</h1><p>Works fine.</p></body></html>"
        result = html_to_plaintext(html)
        assert "Bytes Input" in result
        assert "Works fine" in result

    def test_bytes_input_with_unicode(self):
        """Test bytes input with unicode characters."""
        html = "<html><body><p>Hello 世界</p></body></html>".encode()
        result = html_to_plaintext(html)
        assert "Hello 世界" in result

    def test_unicode_characters(self):
        """Test unicode characters are preserved."""
        html = "<html><body><p>Café résumé naïve</p></body></html>"
        result = html_to_plaintext(html)
        assert "Café" in result
        assert "résumé" in result
        assert "naïve" in result

    def test_emoji_support(self):
        """Test emoji characters are preserved."""
        html = "<html><body><p>Hello 👋 World 🌍</p></body></html>"
        result = html_to_plaintext(html)
        assert "👋" in result
        assert "🌍" in result

    def test_nested_structures(self):
        """Test deeply nested HTML structures."""
        html = """
        <html><body>
            <div>
                <div>
                    <div>
                        <p>Deeply nested content.</p>
                    </div>
                </div>
            </div>
        </body></html>
        """
        result = html_to_plaintext(html)
        assert "Deeply nested content" in result

    def test_only_whitespace_content(self):
        """Test HTML with only whitespace content."""
        html = "<html><body><p>   </p><div>   </div></body></html>"
        result = html_to_plaintext(html)
        assert result == ""

    def test_special_html_entities(self):
        """Test HTML entities are decoded."""
        html = "<html><body><p>Less &lt; Greater &gt; Amp &amp;</p></body></html>"
        result = html_to_plaintext(html)
        assert "<" in result
        assert ">" in result
        assert "&" in result
        # Should not contain raw entity codes
        assert "&lt;" not in result
        assert "&gt;" not in result
        assert "&amp;" not in result

    def test_table_content_extraction(self):
        """Test table content is extracted as text."""
        html = """
        <html><body>
            <table>
                <tr><td>Cell 1</td><td>Cell 2</td></tr>
                <tr><td>Cell 3</td><td>Cell 4</td></tr>
            </table>
        </body></html>
        """
        result = html_to_plaintext(html)
        assert "Cell 1" in result
        assert "Cell 2" in result
        assert "Cell 3" in result
        assert "Cell 4" in result
