"""Root conftest - imports all fixture modules.

This file auto-discovers and loads all fixtures from the fixtures/ directory,
making them available to all tests throughout the test suite.
"""

# Import all fixture modules to make them available globally
pytest_plugins = [
    "tests.fixtures.api_fixtures",
    "tests.fixtures.mock_fixtures",
    "tests.fixtures.data_fixtures",
    "tests.fixtures.env_fixtures",
    "tests.fixtures.html_fixtures",
]
