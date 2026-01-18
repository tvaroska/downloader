"""Root conftest - imports all fixture modules.

This file auto-discovers and loads all fixtures from the fixtures/ directory,
making them available to all tests throughout the test suite.
"""

import pytest

# Import all fixture modules to make them available globally
pytest_plugins = [
    "tests.fixtures.api_fixtures",
    "tests.fixtures.mock_fixtures",
    "tests.fixtures.data_fixtures",
    "tests.fixtures.env_fixtures",
    "tests.fixtures.html_fixtures",
]


def pytest_collection_modifyitems(config, items):
    """Apply timeouts based on test markers to prevent hanging tests."""
    for item in items:
        # E2E tests get longer timeout (120s)
        if item.get_closest_marker("e2e"):
            if not item.get_closest_marker("timeout"):
                item.add_marker(pytest.mark.timeout(120))
        # Integration tests get medium timeout (60s)
        elif item.get_closest_marker("integration"):
            if not item.get_closest_marker("timeout"):
                item.add_marker(pytest.mark.timeout(60))
        # Network tests get medium timeout (45s)
        elif item.get_closest_marker("network"):
            if not item.get_closest_marker("timeout"):
                item.add_marker(pytest.mark.timeout(45))
