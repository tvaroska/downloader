"""REST API Downloader - High-performance web service for programmatic URL content downloading."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("downloader")
except PackageNotFoundError:
    __version__ = "0.0.0"  # Fallback for development
