"""Request and response schemas for download, render, and batch endpoints."""

from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .browser_actions import BrowserAction

BatchFormat = Literal["text", "html", "markdown", "pdf", "screenshot", "json", "raw"]


class ResponseMetadata(TypedDict):
    """Type definition for HTTP response metadata."""

    status_code: int
    headers: dict[str, str]
    url: str
    size: int
    content_type: str
    http_version: str
    connection_reused: bool | None


class ErrorResponse(BaseModel):
    """Response model for errors."""

    success: bool = False
    error: str
    error_type: str


class HTTPErrorResponse(BaseModel):
    """Error response as returned by HTTPException (wrapped in detail key)."""

    detail: ErrorResponse


class BatchURLRequest(BaseModel):
    """Individual URL request within a batch."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(..., min_length=1, max_length=2048, description="URL to download")
    format: BatchFormat | None = Field(
        None,
        description="Desired output format (text, html, markdown, pdf, screenshot, json, raw)",
    )
    custom_headers: dict[str, str] | None = Field(None, description="Custom headers for this URL")
    actions: list[BrowserAction] | None = Field(
        default=None,
        max_length=20,
        description="Optional declarative Playwright actions to execute prior to capture",
    )
    full_page: bool = Field(
        default=False,
        description="Capture full scrollable page when format='screenshot'",
    )
    screenshot_format: Literal["png", "jpeg"] = Field(
        default="png",
        description="Image encoding when format='screenshot'",
    )
    ocr_language: str | None = Field(
        default=None,
        max_length=64,
        pattern=r"^[a-zA-Z0-9_+]{1,64}$",
        description="Optional Tesseract language override when downloading images as text/markdown",
    )


class BatchRequest(BaseModel):
    """Request model for batch processing."""

    model_config = ConfigDict(extra="forbid")

    urls: list[BatchURLRequest] = Field(
        ..., min_length=1, max_length=1000, description="List of URLs to process"
    )
    default_format: BatchFormat = Field(
        "text", description="Default format for URLs without explicit format"
    )
    concurrency_limit: int | None = Field(
        None, ge=1, le=100, description="Maximum concurrent requests"
    )
    timeout_per_url: int | None = Field(
        None, ge=5, le=300, description="Timeout per URL in seconds"
    )
    webhook_url: str | None = Field(
        default=None,
        max_length=2048,
        description="Optional HTTP(S) callback URL invoked upon batch job completion or failure",
    )
    webhook_secret: str | None = Field(
        default=None,
        max_length=256,
        description="Optional HMAC-SHA256 signing secret sent via X-Downloader-Signature-256",
    )

    @field_validator("webhook_secret")
    @classmethod
    def validate_webhook_secret(cls, v: str | None) -> str | None:
        """Reject caller-supplied ciphertext tokens starting with `enc:v1:`."""
        if v is not None and v.startswith("enc:v1:"):
            raise ValueError("webhook_secret must not start with reserved 'enc:v1:' prefix")
        return v


class RenderRequest(BaseModel):
    """Structured browser render request supporting scripted interactions and capture toggles."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(..., min_length=1, max_length=2048, description="Target URL to render")
    output: Literal["html", "text", "markdown", "pdf", "screenshot"] = Field(
        default="html",
        description="Rendered output format",
    )
    wait_for: str | None = Field(
        default=None,
        max_length=500,
        description="Optional CSS selector to wait for before running actions/capture",
    )
    actions: list[BrowserAction] = Field(
        default_factory=list,
        max_length=20,
        description="Declarative browser interactions to execute before capture",
    )
    full_page: bool = Field(
        default=False,
        description="Capture full scrollable page when output='screenshot'",
    )
    screenshot_format: Literal["png", "jpeg"] = Field(
        default="png",
        description="Image format when output='screenshot'",
    )
    timeout_ms: int | None = Field(
        default=None,
        ge=1000,
        le=120000,
        description="Optional render timeout override in milliseconds",
    )


class BatchURLResult(BaseModel):
    """Result for a single URL in a batch."""

    url: str = Field(..., description="Original URL")
    success: bool = Field(..., description="Whether processing succeeded")
    format: str = Field(..., description="Output format used")
    content: str | None = Field(None, description="Processed content (text formats)")
    content_base64: str | None = Field(None, description="Base64 encoded content (binary formats)")
    size: int | None = Field(None, description="Content size in bytes")
    content_type: str | None = Field(None, description="Original content type")
    duration: float | None = Field(None, description="Processing time in seconds")
    error: str | None = Field(None, description="Error message if failed")
    error_type: str | None = Field(None, description="Error type classification")
    status_code: int | None = Field(None, description="HTTP status code")


class JobSubmissionResponse(BaseModel):
    """Response model for job submission."""

    job_id: str = Field(..., description="Unique job identifier")
    status: str = Field(..., description="Initial job status")
    created_at: str = Field(..., description="Job creation timestamp")
    total_urls: int = Field(..., description="Total number of URLs to process")
    estimated_completion: str | None = Field(None, description="Estimated completion time")


class JobStatusResponse(BaseModel):
    """Response model for job status check."""

    job_id: str = Field(..., description="Job identifier")
    status: str = Field(..., description="Current job status")
    progress: int = Field(..., description="Progress percentage (0-100)")
    created_at: str = Field(..., description="Job creation timestamp")
    started_at: str | None = Field(None, description="Job start timestamp")
    completed_at: str | None = Field(None, description="Job completion timestamp")
    total_urls: int = Field(..., description="Total number of URLs to process")
    processed_urls: int = Field(..., description="Number of URLs processed")
    successful_urls: int = Field(..., description="Number of successfully processed URLs")
    failed_urls: int = Field(..., description="Number of failed URLs")
    error_message: str | None = Field(None, description="Error message if job failed")
    results_available: bool = Field(..., description="Whether results are available for download")
    expires_at: str | None = Field(None, description="Job expiration timestamp")
