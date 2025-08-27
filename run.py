#!/usr/bin/env python3
"""Run script for the REST API Downloader."""

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "src.downloader.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
    )