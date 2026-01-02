"""HTML content fixtures for testing JavaScript rendering detection."""

import pytest


@pytest.fixture
def substack_minimal_html():
    """
    Minimal Substack-like HTML without metadata (simulates initial SSR response).

    Characteristics:
    - Missing og:title, og:description, og:image
    - Contains substack.com in URL
    - Small content size (<50KB)
    - Should trigger JS rendering
    """
    return b"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title></title>
    <link rel="stylesheet" href="/styles.css">
    <script src="/bundle.js"></script>
</head>
<body>
    <div id="root"></div>
    <p>Loading...</p>
</body>
</html>"""


@pytest.fixture
def substack_complete_html():
    """
    Complete Substack HTML with all metadata (simulates rendered response).

    Characteristics:
    - Has og:title, og:description, og:image
    - Contains article content
    - Larger content size
    - Should NOT trigger additional JS rendering (already complete)
    """
    return b"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title data-rh="true">Understanding SHACL 1.2 Rules - by Kurt Cagle</title>
    <meta property="og:title" content="Understanding SHACL 1.2 Rules">
    <meta property="og:description" content="Adding an inferencing layer to SHACL">
    <meta property="og:image" content="https://substackcdn.com/image/fetch/s_1200x600/image.jpeg">
    <meta name="twitter:title" content="Understanding SHACL 1.2 Rules">
    <meta name="twitter:description" content="Adding an inferencing layer to SHACL">
</head>
<body>
    <div id="root">
        <article>
            <h1>Understanding SHACL 1.2 Rules</h1>
            <p>This is a detailed article about SHACL with lots of content...</p>
            <p>Lorem ipsum dolor sit amet, consectetur adipiscing elit...</p>
            <p>More content here to make it substantial...</p>
        </article>
    </div>
</body>
</html>"""


@pytest.fixture
def react_app_minimal_html():
    """
    React app with minimal content and framework markers.

    Characteristics:
    - Has #root div
    - Minimal body text
    - Missing metadata
    - Should trigger JS rendering
    """
    return b"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>React App</title>
</head>
<body>
    <div id="root"></div>
    <script src="/static/js/main.js"></script>
</body>
</html>"""


@pytest.fixture
def vue_app_minimal_html():
    """
    Vue app with minimal content and framework markers.

    Characteristics:
    - Has #app div
    - Minimal body text
    - Missing metadata
    - Should trigger JS rendering
    """
    return b"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Vue App</title>
</head>
<body>
    <div id="app"></div>
    <script src="/dist/app.js"></script>
</body>
</html>"""


@pytest.fixture
def angular_app_minimal_html():
    """
    Angular app with minimal content and framework markers.

    Characteristics:
    - Has ng-app attribute
    - Minimal body text
    - Missing metadata
    - Should trigger JS rendering
    """
    return b"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Angular App</title>
</head>
<body ng-app="myApp">
    <div ng-view></div>
    <script src="/angular.js"></script>
</body>
</html>"""


@pytest.fixture
def static_html_complete():
    """
    Complete static HTML with metadata and substantial content.

    Characteristics:
    - Has og:title, og:description
    - Substantial body text (>500 chars)
    - No JS framework markers
    - Should NOT trigger JS rendering
    """
    return b"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Static Blog Post</title>
    <meta property="og:title" content="Static Blog Post">
    <meta property="og:description" content="A complete static HTML page">
    <meta name="twitter:title" content="Static Blog Post">
    <meta name="twitter:description" content="A complete static HTML page">
</head>
<body>
    <main>
        <article>
            <h1>Static Blog Post</h1>
            <p>This is a complete static HTML page with plenty of content.</p>
            <p>Lorem ipsum dolor sit amet, consectetur adipiscing elit.
            Sed do eiusmod tempor incididunt ut labore et dolore magna aliqua.</p>
            <p>Ut enim ad minim veniam, quis nostrud exercitation ullamco
            laboris nisi ut aliquip ex ea commodo consequat.</p>
            <p>Duis aute irure dolor in reprehenderit in voluptate velit
            esse cillum dolore eu fugiat nulla pariatur.</p>
            <p>Excepteur sint occaecat cupidatat non proident, sunt in
            culpa qui officia deserunt mollit anim id est laborum.</p>
        </article>
    </main>
</body>
</html>"""


@pytest.fixture
def js_required_message_html():
    """
    HTML with explicit JavaScript requirement message.

    Characteristics:
    - Contains "please enable javascript" message
    - Should trigger JS rendering
    """
    return b"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Site</title>
</head>
<body>
    <noscript>
        <p>Please enable JavaScript to view this site.</p>
    </noscript>
    <div id="app"></div>
</body>
</html>"""


@pytest.fixture
def empty_html():
    """
    Nearly empty HTML (edge case).

    Characteristics:
    - Minimal content
    - Should NOT trigger JS rendering (nothing to render)
    """
    return b"""<!DOCTYPE html>
<html>
<head><title>Empty</title></head>
<body></body>
</html>"""


@pytest.fixture
def malformed_html():
    """
    Malformed HTML (edge case).

    Characteristics:
    - Invalid HTML structure
    - Should handle gracefully
    """
    return b"""<html><body><div>Unclosed tags<p>Malformed</html>"""


@pytest.fixture
def medium_minimal_html():
    """
    Medium.com-like minimal HTML.

    Characteristics:
    - Contains medium.com domain
    - Missing metadata
    - Should trigger JS rendering (known JS-heavy domain)
    """
    return b"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title></title>
</head>
<body>
    <div id="root">
        <p>Loading Medium post...</p>
    </div>
</body>
</html>"""


@pytest.fixture
def large_html_with_metadata():
    """
    Large HTML (>50KB) with complete metadata.

    Characteristics:
    - Size > 50KB threshold
    - Has complete metadata
    - Should NOT trigger JS rendering (already complete)
    """
    # Create large content by repeating paragraphs
    large_content = "<p>Lorem ipsum dolor sit amet, consectetur adipiscing elit. </p>" * 1000

    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Large Article</title>
    <meta property="og:title" content="Large Article">
    <meta property="og:description" content="A very large article with lots of content">
    <meta property="og:image" content="https://example.com/image.jpg">
</head>
<body>
    <article>
        <h1>Large Article</h1>
        {large_content}
    </article>
</body>
</html>""".encode()
