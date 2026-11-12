package downloaderclient

import (
	"fmt"
	"net/http"
)

// BrowserAction represents a declarative allowlisted browser interaction executed prior to capture.
type BrowserAction struct {
	Type       string `json:"type"`
	Selector   string `json:"selector,omitempty"`
	Value      string `json:"value,omitempty"`
	Key        string `json:"key,omitempty"`
	X          int    `json:"x,omitempty"`
	Y          int    `json:"y,omitempty"`
	TimeoutMs  int    `json:"timeout_ms,omitempty"`
	DurationMs int    `json:"duration_ms,omitempty"`
}

// DownloadOptions configures a GET /{url} request.
type DownloadOptions struct {
	Format           string
	Render           bool
	WaitFor          string
	Output           string // "screenshot" or "pdf"
	FullPage         bool
	ScreenshotFormat string // "png" or "jpeg"
	Actions          []BrowserAction
	OCR              bool
	OCRLang          string
}

// DownloadResult holds response bytes and headers from a download, render, screenshot, PDF, or OCR call.
type DownloadResult struct {
	URL            string
	StatusCode     int
	ContentType    string
	Body           []byte
	Headers        http.Header
	RenderedWithJS bool
	OCRApplied     bool
	TraceID        string
}

// Text decodes the response body as a UTF-8 string.
func (r *DownloadResult) Text() string {
	return string(r.Body)
}

// RenderRequest is the payload for POST /render.
type RenderRequest struct {
	URL              string          `json:"url"`
	Output           string          `json:"output,omitempty"`
	WaitFor          string          `json:"wait_for,omitempty"`
	Actions          []BrowserAction `json:"actions,omitempty"`
	FullPage         bool            `json:"full_page,omitempty"`
	ScreenshotFormat string          `json:"screenshot_format,omitempty"`
}

// BatchURLItem represents one URL inside a batch submission.
type BatchURLItem struct {
	URL              string            `json:"url"`
	Format           string            `json:"format,omitempty"`
	CustomHeaders    map[string]string `json:"custom_headers,omitempty"`
	Actions          []BrowserAction   `json:"actions,omitempty"`
	FullPage         bool              `json:"full_page,omitempty"`
	ScreenshotFormat string            `json:"screenshot_format,omitempty"`
	OCRLanguage      string            `json:"ocr_language,omitempty"`
}

// BatchRequest is the payload for POST /batch.
type BatchRequest struct {
	URLs             []BatchURLItem `json:"urls"`
	DefaultFormat    string         `json:"default_format,omitempty"`
	ConcurrencyLimit *int           `json:"concurrency_limit,omitempty"`
	TimeoutPerURL    *int           `json:"timeout_per_url,omitempty"`
	WebhookURL       string         `json:"webhook_url,omitempty"`
	WebhookSecret    string         `json:"webhook_secret,omitempty"`
}

// BatchJobSubmission is returned by POST /batch.
type BatchJobSubmission struct {
	JobID               string  `json:"job_id"`
	Status              string  `json:"status"`
	CreatedAt           string  `json:"created_at"`
	TotalURLs           int     `json:"total_urls"`
	EstimatedCompletion *string `json:"estimated_completion,omitempty"`
}

// BatchJobStatus is returned by GET /jobs/{job_id}/status.
type BatchJobStatus struct {
	JobID            string  `json:"job_id"`
	Status           string  `json:"status"`
	Progress         int     `json:"progress"`
	CreatedAt        string  `json:"created_at"`
	StartedAt        *string `json:"started_at,omitempty"`
	CompletedAt      *string `json:"completed_at,omitempty"`
	TotalURLs        int     `json:"total_urls"`
	ProcessedURLs    int     `json:"processed_urls"`
	SuccessfulURLs   int     `json:"successful_urls"`
	FailedURLs       int     `json:"failed_urls"`
	ErrorMessage     *string `json:"error_message,omitempty"`
	ResultsAvailable bool    `json:"results_available"`
}

// BatchJobResults is returned by GET /jobs/{job_id}/results.
type BatchJobResults struct {
	JobID         string           `json:"job_id"`
	Status        string           `json:"status"`
	TotalDuration float64          `json:"total_duration"`
	Results       []map[string]any `json:"results"`
	Summary       map[string]any   `json:"summary"`
	CreatedAt     string           `json:"created_at"`
	CompletedAt   string           `json:"completed_at"`
}

// ScheduleCreateRequest is the payload for POST /schedules.
type ScheduleCreateRequest struct {
	Name           string            `json:"name"`
	URL            string            `json:"url"`
	CronExpression string            `json:"cron_expression"`
	Format         string            `json:"format,omitempty"`
	Headers        map[string]string `json:"headers,omitempty"`
	Enabled        *bool             `json:"enabled,omitempty"`
	WebhookURL     string            `json:"webhook_url,omitempty"`
	WebhookSecret  string            `json:"webhook_secret,omitempty"`
}

// ScheduleInfo represents a scheduled job returned by the API.
type ScheduleInfo struct {
	ID             string            `json:"id"`
	Name           string            `json:"name"`
	URL            string            `json:"url"`
	CronExpression string            `json:"cron_expression"`
	Format         string            `json:"format"`
	Headers        map[string]string `json:"headers,omitempty"`
	Enabled        bool              `json:"enabled"`
	CreatedAt      string            `json:"created_at"`
	NextRunTime    *string           `json:"next_run_time,omitempty"`
	WebhookURL     *string           `json:"webhook_url,omitempty"`
}

// HealthStatus is returned by GET /health.
type HealthStatus struct {
	Status           string         `json:"status"`
	Version          string         `json:"version"`
	Environment      string         `json:"environment"`
	Draining         bool           `json:"draining"`
	ActiveRequests   int            `json:"active_requests"`
	DeploymentMode   string         `json:"deployment_mode"`
	TelemetryEnabled bool           `json:"telemetry_enabled"`
	Configuration    map[string]any `json:"configuration,omitempty"`
	Services         map[string]any `json:"services,omitempty"`
}

// APIError captures structured HTTP error responses from the Downloader API.
type APIError struct {
	StatusCode int
	ErrorType  string
	Message    string
	RetryAfter string
}

func (e *APIError) Error() string {
	if e.ErrorType != "" {
		return fmt.Sprintf("downloader api error (%d %s): %s", e.StatusCode, e.ErrorType, e.Message)
	}
	return fmt.Sprintf("downloader api error (%d): %s", e.StatusCode, e.Message)
}
