"""Response models for API endpoints."""

from typing import TypedDict

from pydantic import BaseModel, Field


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


# Batch processing models
class BatchURLRequest(BaseModel):
    """Individual URL request within a batch."""
    url: str = Field(..., description="URL to download")
    format: str | None = Field(
        None, description="Desired output format (text, html, markdown, pdf, json, raw)"
    )
    custom_headers: dict[str, str] | None = Field(
        None, description="Custom headers for this URL"
    )


class BatchRequest(BaseModel):
    """Request model for batch processing."""
    urls: list[BatchURLRequest] = Field(
        ..., min_length=1, max_length=50, description="List of URLs to process"
    )
    default_format: str = Field(
        "text", description="Default format for URLs without explicit format"
    )
    concurrency_limit: int | None = Field(
        10, ge=1, le=20, description="Maximum concurrent requests"
    )
    timeout_per_url: int | None = Field(
        30, ge=5, le=120, description="Timeout per URL in seconds"
    )


class BatchURLResult(BaseModel):
    """Result for a single URL in a batch."""
    url: str = Field(..., description="Original URL")
    success: bool = Field(..., description="Whether processing succeeded")
    format: str = Field(..., description="Output format used")
    content: str | None = Field(None, description="Processed content (text formats)")
    content_base64: str | None = Field(
        None, description="Base64 encoded content (binary formats)"
    )
    size: int | None = Field(None, description="Content size in bytes")
    content_type: str | None = Field(None, description="Original content type")
    duration: float | None = Field(None, description="Processing time in seconds")
    error: str | None = Field(None, description="Error message if failed")
    error_type: str | None = Field(None, description="Error type classification")
    status_code: int | None = Field(None, description="HTTP status code")


class BatchResponse(BaseModel):
    """Response model for batch processing."""
    success: bool = Field(..., description="Overall batch success")
    total_requests: int = Field(..., description="Total number of URLs processed")
    successful_requests: int = Field(..., description="Number of successful requests")
    failed_requests: int = Field(..., description="Number of failed requests")
    success_rate: float = Field(..., description="Success rate as percentage")
    total_duration: float = Field(..., description="Total processing time in seconds")
    results: list[BatchURLResult] = Field(..., description="Individual URL results")
    batch_id: str | None = Field(None, description="Unique batch identifier")


# Job-based models
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


# Concurrency stats models
class ConcurrencyInfo(BaseModel):
    """Model for concurrency information of a specific service."""
    limit: int = Field(..., description="Maximum concurrent operations allowed")
    available: int = Field(..., description="Currently available slots")
    in_use: int = Field(..., description="Currently used slots")
    utilization_percent: float = Field(..., description="Utilization percentage (0-100)")


class SystemInfo(BaseModel):
    """Model for system information."""
    cpu_cores: int = Field(..., description="Number of CPU cores")
    pdf_scaling_factor: str = Field(..., description="PDF concurrency scaling factor")
    batch_scaling_factor: str = Field(..., description="Batch concurrency scaling factor")


class ConcurrencyStats(BaseModel):
    """Model for overall concurrency statistics."""
    pdf_concurrency: ConcurrencyInfo = Field(..., description="PDF generation concurrency stats")
    batch_concurrency: ConcurrencyInfo = Field(..., description="Batch processing concurrency stats")
    system_info: SystemInfo = Field(..., description="System information")
