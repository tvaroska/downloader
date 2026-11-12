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

// Client is a typed stdlib HTTP client for the REST API Downloader v0.7.0+ service.
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

// Download fetches a URL with optional content negotiation, screenshot/PDF toggle, actions, or OCR.
func (c *Client) Download(ctx context.Context, targetURL string, opts *DownloadOptions) (*DownloadResult, error) {
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
			return nil, err
		}
		q.Set("actions", string(encoded))
	}
	if opts.OCR {
		q.Set("ocr", "true")
	}
	if opts.OCRLang != "" {
		q.Set("ocr_lang", opts.OCRLang)
	}

	endpoint := c.BaseURL + "/" + strings.TrimLeft(targetURL, "/")
	if encoded := q.Encode(); encoded != "" {
		endpoint += "?" + encoded
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
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.BaseURL+"/jobs/"+url.PathEscape(jobID)+"/status", nil)
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
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.BaseURL+"/jobs/"+url.PathEscape(jobID)+"/results", nil)
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

// CreateSchedule calls POST /schedules.
func (c *Client) CreateSchedule(ctx context.Context, payload ScheduleCreateRequest) (*ScheduleInfo, error) {
	raw, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.BaseURL+"/schedules", bytes.NewReader(raw))
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
	var out ScheduleInfo
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, err
	}
	return &out, nil
}
