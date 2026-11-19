package downloaderclient

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

var formatToAccept = map[string]string{
	"text":            "text/plain",
	"markdown":        "text/markdown",
	"html":            "text/html",
	"pdf":             "application/pdf",
	"screenshot":      "image/png",
	"screenshot_jpeg": "image/jpeg",
	"json":            "application/json",
	"raw":             "*/*",
}

// Client is a typed stdlib HTTP client for the REST API Downloader service.
type Client struct {
	BaseURL    string
	APIKey     string
	HTTPClient *http.Client
}

// New creates a configured Client with sensible defaults.
func New(baseURL string, apiKey string) *Client {
	return &Client{
		BaseURL: strings.TrimRight(baseURL, "/"),
		APIKey:  apiKey,
		HTTPClient: &http.Client{
			Timeout: 60 * time.Second,
		},
	}
}

func (c *Client) httpClient() *http.Client {
	if c.HTTPClient != nil {
		return c.HTTPClient
	}
	return http.DefaultClient
}

func (c *Client) setAuth(req *http.Request) {
	if c.APIKey != "" {
		req.Header.Set("X-API-Key", c.APIKey)
	}
}

func decodeAPIError(resp *http.Response, body []byte) error {
	apiErr := &APIError{
		StatusCode: resp.StatusCode,
		Message:    fmt.Sprintf("HTTP %d", resp.StatusCode),
		RetryAfter: resp.Header.Get("Retry-After"),
	}
	var envelope struct {
		Detail struct {
			Error     string `json:"error"`
			ErrorType string `json:"error_type"`
		} `json:"detail"`
	}
	if err := json.Unmarshal(body, &envelope); err == nil && envelope.Detail.Error != "" {
		apiErr.Message = envelope.Detail.Error
		apiErr.ErrorType = envelope.Detail.ErrorType
	} else if len(body) > 0 {
		trimmed := strings.TrimSpace(string(body))
		if len(trimmed) > 300 {
			trimmed = trimmed[:300]
		}
		apiErr.Message = trimmed
	}
	return apiErr
}

// Health calls GET /health.
func (c *Client) Health(ctx context.Context) (*HealthStatus, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.BaseURL+"/health", nil)
	if err != nil {
		return nil, err
	}
	c.setAuth(req)

	resp, err := c.httpClient().Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode >= 400 {
		return nil, decodeAPIError(resp, body)
	}
	var status HealthStatus
	if err := json.Unmarshal(body, &status); err != nil {
		return nil, err
	}
	return &status, nil
}

func encodeTargetURLPath(targetURL string) string {
	trimmed := strings.TrimLeft(targetURL, "/")
	r := strings.NewReplacer("%", "%25", "?", "%3F", "#", "%23")
	return r.Replace(trimmed)
}

func escapePathSegment(value string, field string) (string, error) {
	if value == "" || value == "." || value == ".." {
		return "", fmt.Errorf("%s must be a non-empty path segment and not '.' or '..'", field)
	}
	return url.PathEscape(value), nil
}

func (c *Client) buildDownloadEndpoint(targetURL string, opts *DownloadOptions) (string, error) {
	if opts == nil {
		opts = &DownloadOptions{}
	}
	q := url.Values{}
	if opts.Render {
		q.Set("render", "true")
	}
	if opts.WaitFor != "" {
		q.Set("wait_for", opts.WaitFor)
	}
	if opts.Output != "" {
		q.Set("output", opts.Output)
	}
	if opts.FullPage {
		q.Set("full_page", "true")
	}
	if opts.ScreenshotFormat != "" && opts.ScreenshotFormat != "png" {
		q.Set("screenshot_format", opts.ScreenshotFormat)
	}
	if len(opts.Actions) > 0 {
		encoded, err := json.Marshal(opts.Actions)
		if err != nil {
			return "", err
		}
		q.Set("actions", string(encoded))
	}
	if opts.OCR {
		q.Set("ocr", "true")
	}
	if opts.OCRLang != "" {
		q.Set("ocr_lang", opts.OCRLang)
	}

	endpoint := c.BaseURL + "/" + encodeTargetURLPath(targetURL)
	if encoded := q.Encode(); encoded != "" {
		endpoint += "?" + encoded
	}
	return endpoint, nil
}

func (c *Client) doJSON(ctx context.Context, method string, endpoint string, reqBody []byte, out any) error {
	var bodyReader io.Reader
	if reqBody != nil {
		bodyReader = bytes.NewReader(reqBody)
	}
	req, err := http.NewRequestWithContext(ctx, method, endpoint, bodyReader)
	if err != nil {
		return err
	}
	if reqBody != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	c.setAuth(req)

	resp, err := c.httpClient().Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return err
	}
	if resp.StatusCode >= 400 {
		return decodeAPIError(resp, body)
	}
	return json.Unmarshal(body, out)
}

// Download fetches a URL with optional content negotiation, screenshot/PDF toggle, actions, or OCR.
func (c *Client) Download(ctx context.Context, targetURL string, opts *DownloadOptions) (*DownloadResult, error) {
	if opts == nil {
		opts = &DownloadOptions{}
	}
	endpoint, err := c.buildDownloadEndpoint(targetURL, opts)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return nil, err
	}
	accept := "text/plain"
	if opts.Format != "" {
		if mapped, ok := formatToAccept[opts.Format]; ok {
			accept = mapped
		} else {
			accept = opts.Format
		}
	}
	req.Header.Set("Accept", accept)
	c.setAuth(req)

	resp, err := c.httpClient().Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode >= 400 {
		return nil, decodeAPIError(resp, body)
	}
	return &DownloadResult{
		URL:            targetURL,
		StatusCode:     resp.StatusCode,
		ContentType:    resp.Header.Get("Content-Type"),
		Body:           body,
		Headers:        resp.Header.Clone(),
		RenderedWithJS: strings.EqualFold(resp.Header.Get("X-Rendered-With-JS"), "true"),
		OCRApplied:     strings.EqualFold(resp.Header.Get("X-OCR-Applied"), "true"),
		TraceID:        resp.Header.Get("X-Trace-ID"),
	}, nil
}

// DownloadMulti fetches a URL in multiple formats using comma-separated Accept content negotiation.
func (c *Client) DownloadMulti(ctx context.Context, targetURL string, formats []string, opts *DownloadOptions) (map[string]any, error) {
	endpoint, err := c.buildDownloadEndpoint(targetURL, opts)
	if err != nil {
		return nil, err
	}
	acceptParts := make([]string, 0, len(formats))
	for _, f := range formats {
		if mapped, ok := formatToAccept[f]; ok {
			acceptParts = append(acceptParts, mapped)
		} else {
			acceptParts = append(acceptParts, f)
		}
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", strings.Join(acceptParts, ", "))
	c.setAuth(req)

	resp, err := c.httpClient().Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode >= 400 {
		return nil, decodeAPIError(resp, body)
	}
	ct := strings.ToLower(strings.TrimSpace(strings.Split(resp.Header.Get("Content-Type"), ";")[0]))
	if ct != "" && ct != "application/json" && !strings.HasSuffix(ct, "+json") {
		return nil, &APIError{
			StatusCode: resp.StatusCode,
			ErrorType:  "unexpected_content_type",
			Message:    fmt.Sprintf("expected JSON multi-format response, got %s", ct),
		}
	}
	var out map[string]any
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, &APIError{
			StatusCode: resp.StatusCode,
			ErrorType:  "invalid_json_response",
			Message:    fmt.Sprintf("expected JSON multi-format response: %v", err),
		}
	}
	return out, nil
}

// Screenshot captures a PNG or JPEG screenshot of targetURL.
func (c *Client) Screenshot(ctx context.Context, targetURL string, fullPage bool, format string, actions []BrowserAction) (*DownloadResult, error) {
	return c.Download(ctx, targetURL, &DownloadOptions{
		Output:           "screenshot",
		FullPage:         fullPage,
		ScreenshotFormat: format,
		Actions:          actions,
	})
}

// Render calls POST /render with structured declarative actions.
func (c *Client) Render(ctx context.Context, payload RenderRequest) (*DownloadResult, error) {
	if payload.Output == "" {
		payload.Output = "html"
	}
	raw, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.BaseURL+"/render", bytes.NewReader(raw))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	c.setAuth(req)

	resp, err := c.httpClient().Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode >= 400 {
		return nil, decodeAPIError(resp, body)
	}
	return &DownloadResult{
		URL:            payload.URL,
		StatusCode:     resp.StatusCode,
		ContentType:    resp.Header.Get("Content-Type"),
		Body:           body,
		Headers:        resp.Header.Clone(),
		RenderedWithJS: strings.EqualFold(resp.Header.Get("X-Rendered-With-JS"), "true"),
		OCRApplied:     strings.EqualFold(resp.Header.Get("X-OCR-Applied"), "true"),
		TraceID:        resp.Header.Get("X-Trace-ID"),
	}, nil
}

// SubmitBatch calls POST /batch.
func (c *Client) SubmitBatch(ctx context.Context, payload BatchRequest) (*BatchJobSubmission, error) {
	if payload.DefaultFormat == "" {
		payload.DefaultFormat = "text"
	}
	raw, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.BaseURL+"/batch", bytes.NewReader(raw))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	c.setAuth(req)

	resp, err := c.httpClient().Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode >= 400 {
		return nil, decodeAPIError(resp, body)
	}
	var out BatchJobSubmission
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// GetJobStatus calls GET /jobs/{job_id}/status.
func (c *Client) GetJobStatus(ctx context.Context, jobID string) (*BatchJobStatus, error) {
	encoded, err := escapePathSegment(jobID, "jobID")
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.BaseURL+"/jobs/"+encoded+"/status", nil)
	if err != nil {
		return nil, err
	}
	c.setAuth(req)
	resp, err := c.httpClient().Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode >= 400 {
		return nil, decodeAPIError(resp, body)
	}
	var out BatchJobStatus
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// GetJobResults calls GET /jobs/{job_id}/results.
func (c *Client) GetJobResults(ctx context.Context, jobID string) (*BatchJobResults, error) {
	encoded, err := escapePathSegment(jobID, "jobID")
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.BaseURL+"/jobs/"+encoded+"/results", nil)
	if err != nil {
		return nil, err
	}
	c.setAuth(req)
	resp, err := c.httpClient().Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode >= 400 {
		return nil, decodeAPIError(resp, body)
	}
	var out BatchJobResults
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// CancelJob calls DELETE /jobs/{job_id}.
func (c *Client) CancelJob(ctx context.Context, jobID string) (*JobCancelResult, error) {
	encoded, err := escapePathSegment(jobID, "jobID")
	if err != nil {
		return nil, err
	}
	var out JobCancelResult
	endpoint := c.BaseURL + "/jobs/" + encoded
	if err := c.doJSON(ctx, http.MethodDelete, endpoint, nil, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// WaitForBatch polls GET /jobs/{job_id}/status until completion, failure, cancellation, or context expiry.
func (c *Client) WaitForBatch(ctx context.Context, jobID string, pollInterval time.Duration) (*BatchJobResults, error) {
	if pollInterval <= 0 {
		pollInterval = time.Second
	}
	for {
		status, err := c.GetJobStatus(ctx, jobID)
		if err != nil {
			return nil, err
		}
		if status.Status == "completed" {
			return c.GetJobResults(ctx, jobID)
		}
		if status.Status == "failed" || status.Status == "cancelled" {
			msg := fmt.Sprintf("Batch job %s ended with status %s", jobID, status.Status)
			if status.ErrorMessage != nil && *status.ErrorMessage != "" {
				msg = *status.ErrorMessage
			}
			return nil, &BatchJobError{
				JobID:     jobID,
				JobStatus: status.Status,
				ErrorType: "batch_" + status.Status,
				Message:   msg,
			}
		}
		timer := time.NewTimer(pollInterval)
		select {
		case <-ctx.Done():
			timer.Stop()
			return nil, ctx.Err()
		case <-timer.C:
		}
	}
}

// CreateSchedule calls POST /schedules.
func (c *Client) CreateSchedule(ctx context.Context, payload ScheduleCreateRequest) (*ScheduleInfo, error) {
	raw, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}
	var out ScheduleInfo
	if err := c.doJSON(ctx, http.MethodPost, c.BaseURL+"/schedules", raw, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// ListSchedules calls GET /schedules.
func (c *Client) ListSchedules(ctx context.Context) ([]ScheduleInfo, error) {
	var wrapper struct {
		Schedules []ScheduleInfo `json:"schedules"`
	}
	if err := c.doJSON(ctx, http.MethodGet, c.BaseURL+"/schedules", nil, &wrapper); err != nil {
		return nil, err
	}
	return wrapper.Schedules, nil
}

// GetSchedule calls GET /schedules/{id}.
func (c *Client) GetSchedule(ctx context.Context, scheduleID string) (*ScheduleInfo, error) {
	encoded, err := escapePathSegment(scheduleID, "scheduleID")
	if err != nil {
		return nil, err
	}
	var out ScheduleInfo
	endpoint := c.BaseURL + "/schedules/" + encoded
	if err := c.doJSON(ctx, http.MethodGet, endpoint, nil, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// GetScheduleHistory calls GET /schedules/{id}/history.
func (c *Client) GetScheduleHistory(ctx context.Context, scheduleID string, limit int, offset int) (*ScheduleHistory, error) {
	encoded, err := escapePathSegment(scheduleID, "scheduleID")
	if err != nil {
		return nil, err
	}
	if limit <= 0 {
		limit = 20
	}
	if offset < 0 {
		offset = 0
	}
	q := url.Values{}
	q.Set("limit", fmt.Sprintf("%d", limit))
	q.Set("offset", fmt.Sprintf("%d", offset))
	endpoint := c.BaseURL + "/schedules/" + encoded + "/history?" + q.Encode()
	var out ScheduleHistory
	if err := c.doJSON(ctx, http.MethodGet, endpoint, nil, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// DeleteSchedule calls DELETE /schedules/{id}.
func (c *Client) DeleteSchedule(ctx context.Context, scheduleID string) (*ScheduleDeleteResult, error) {
	encoded, err := escapePathSegment(scheduleID, "scheduleID")
	if err != nil {
		return nil, err
	}
	var out ScheduleDeleteResult
	endpoint := c.BaseURL + "/schedules/" + encoded
	if err := c.doJSON(ctx, http.MethodDelete, endpoint, nil, &out); err != nil {
		return nil, err
	}
	return &out, nil
}
