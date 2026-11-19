package downloaderclient

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestDownloaderClientEndToEnd(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-API-Key") != "go-secret" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		switch r.URL.Path {
		case "/health":
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"status":"healthy","version":"0.7.1","environment":"test","draining":false}`))
		case "/render":
			var body RenderRequest
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Fatalf("decode render body: %v", err)
			}
			if body.Output != "markdown" || len(body.Actions) != 1 || body.TimeoutMs != 5000 {
				t.Fatalf("unexpected render payload: %+v", body)
			}
			w.Header().Set("Content-Type", "text/markdown")
			w.Header().Set("X-Rendered-With-JS", "true")
			_, _ = w.Write([]byte("# Rendered Go"))
		case "/batch":
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"job_id":"job-go-1","status":"pending","created_at":"2026-11-12T00:00:00Z","total_urls":1}`))
		case "/jobs/job-go-1/status":
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"job_id":"job-go-1","status":"completed","progress":100,"total_urls":1,"processed_urls":1,"successful_urls":1,"failed_urls":0,"results_available":true,"expires_at":"2026-11-13T00:00:00Z"}`))
		case "/jobs/job-cancelled/status":
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"job_id":"job-cancelled","status":"cancelled","progress":50,"total_urls":2,"processed_urls":1,"successful_urls":1,"failed_urls":0,"results_available":false}`))
		case "/jobs/job-go-1/results":
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"job_id":"job-go-1","status":"completed","total_duration":0.3,"results":[{"url":"https://example.com","success":true}],"summary":{"successful_requests":1},"created_at":"2026-11-12T00:00:00Z","completed_at":"2026-11-12T00:00:01Z"}`))
		case "/jobs/job-go-1":
			if r.Method != http.MethodDelete {
				w.WriteHeader(http.StatusMethodNotAllowed)
				return
			}
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"success":true,"message":"Job job-go-1 cancelled successfully"}`))
		case "/schedules":
			w.Header().Set("Content-Type", "application/json")
			if r.Method == http.MethodGet {
				_, _ = w.Write([]byte(`{"schedules":[{"id":"sched-go-1","name":"Hourly","url":"https://example.com","cron_expression":"0 * * * *","format":"screenshot","enabled":true,"created_at":"2026-11-12T00:00:00Z"}],"total":1}`))
				return
			}
			_, _ = w.Write([]byte(`{"id":"sched-go-1","name":"Hourly","url":"https://example.com","cron_expression":"0 * * * *","format":"screenshot","enabled":true,"created_at":"2026-11-12T00:00:00Z"}`))
		case "/schedules/sched-go-1":
			w.Header().Set("Content-Type", "application/json")
			if r.Method == http.MethodDelete {
				_, _ = w.Write([]byte(`{"success":true,"message":"Schedule sched-go-1 deleted successfully"}`))
				return
			}
			_, _ = w.Write([]byte(`{"id":"sched-go-1","name":"Hourly","url":"https://example.com","cron_expression":"0 * * * *","format":"screenshot","enabled":true,"created_at":"2026-11-12T00:00:00Z"}`))
		case "/schedules/sched-go-1/history":
			if r.URL.Query().Get("limit") != "10" || r.URL.Query().Get("offset") != "2" {
				t.Fatalf("unexpected history query: %s", r.URL.RawQuery)
			}
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"executions":[{"execution_id":"exec-go-1","schedule_id":"sched-go-1","status":"completed","started_at":"2026-11-12T00:00:00Z","success":true,"attempt":1}],"total":9,"limit":10,"offset":2}`))
		case "/https://example.com/multi":
			if r.Header.Get("Accept") == "text/plain, text/markdown" {
				w.Header().Set("Content-Type", "application/json")
				_, _ = w.Write([]byte(`{"url":"https://example.com/multi","formats":{"text":"Hello","markdown":"# Hello"}}`))
				return
			}
			w.Header().Set("Content-Type", "text/plain; charset=utf-8")
			_, _ = w.Write([]byte("single format text"))
		case "/https://example.com/shot":
			if r.URL.Query().Get("output") != "screenshot" {
				t.Fatalf("expected output=screenshot, got %s", r.URL.RawQuery)
			}
			w.Header().Set("Content-Type", "image/png")
			_, _ = w.Write([]byte{0x89, 'P', 'N', 'G'})
		case "/https://blocked.local":
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusBadRequest)
			_, _ = w.Write([]byte(`{"detail":{"error":"SSRF blocked","error_type":"validation_error"}}`))
		default:
			w.Header().Set("Content-Type", "text/plain")
			w.Header().Set("X-OCR-Applied", "true")
			_, _ = w.Write([]byte("OK"))
		}
	}))
	defer srv.Close()

	client := New(srv.URL, "go-secret")
	ctx := context.Background()

	health, err := client.Health(ctx)
	if err != nil {
		t.Fatalf("Health failed: %v", err)
	}
	if health.Version != "0.7.1" {
		t.Fatalf("expected version 0.7.1, got %s", health.Version)
	}

	shot, err := client.Screenshot(ctx, "https://example.com/shot", true, "png", nil)
	if err != nil {
		t.Fatalf("Screenshot failed: %v", err)
	}
	if len(shot.Body) != 4 || shot.Body[0] != 0x89 {
		t.Fatalf("unexpected screenshot bytes: %v", shot.Body)
	}

	rendered, err := client.Render(ctx, RenderRequest{
		URL:       "https://example.com",
		Output:    "markdown",
		Actions:   []BrowserAction{{Type: "click", Selector: "#go"}},
		TimeoutMs: 5000,
	})
	if err != nil {
		t.Fatalf("Render failed: %v", err)
	}
	if !rendered.RenderedWithJS || rendered.Text() != "# Rendered Go" {
		t.Fatalf("unexpected render result: %+v", rendered)
	}

	sub, err := client.SubmitBatch(ctx, BatchRequest{
		URLs:       []BatchURLItem{{URL: "https://example.com", Format: "markdown"}},
		WebhookURL: "https://hooks.example.com/done",
	})
	if err != nil || sub.JobID != "job-go-1" {
		t.Fatalf("SubmitBatch failed: sub=%+v err=%v", sub, err)
	}
	st, err := client.GetJobStatus(ctx, sub.JobID)
	if err != nil || !st.ResultsAvailable || st.ExpiresAt == nil || *st.ExpiresAt != "2026-11-13T00:00:00Z" {
		t.Fatalf("GetJobStatus failed: st=%+v err=%v", st, err)
	}
	res, err := client.WaitForBatch(ctx, sub.JobID, 5*time.Millisecond)
	if err != nil || len(res.Results) != 1 {
		t.Fatalf("WaitForBatch failed: res=%+v err=%v", res, err)
	}
	_, err = client.WaitForBatch(ctx, "job-cancelled", 5*time.Millisecond)
	var batchErr *BatchJobError
	if !errors.As(err, &batchErr) || batchErr.JobStatus != "cancelled" || batchErr.ErrorType != "batch_cancelled" {
		t.Fatalf("expected BatchJobError for cancelled job, got %v", err)
	}
	if _, err := client.GetJobStatus(ctx, ".."); err == nil {
		t.Fatalf("expected error for traversal job ID '..'")
	}
	cancelled, err := client.CancelJob(ctx, sub.JobID)
	if err != nil || !cancelled.Success {
		t.Fatalf("CancelJob failed: cancelled=%+v err=%v", cancelled, err)
	}

	sched, err := client.CreateSchedule(ctx, ScheduleCreateRequest{
		Name:           "Hourly",
		URL:            "https://example.com",
		CronExpression: "0 * * * *",
		Format:         "screenshot",
	})
	if err != nil || sched.ID != "sched-go-1" {
		t.Fatalf("CreateSchedule failed: sched=%+v err=%v", sched, err)
	}
	schedList, err := client.ListSchedules(ctx)
	if err != nil || len(schedList) != 1 || schedList[0].ID != "sched-go-1" {
		t.Fatalf("ListSchedules failed: list=%+v err=%v", schedList, err)
	}
	schedDetail, err := client.GetSchedule(ctx, "sched-go-1")
	if err != nil || schedDetail.Name != "Hourly" {
		t.Fatalf("GetSchedule failed: sched=%+v err=%v", schedDetail, err)
	}
	history, err := client.GetScheduleHistory(ctx, "sched-go-1", 10, 2)
	if err != nil || history.Total != 9 || len(history.Executions) != 1 || history.Executions[0].ExecutionID != "exec-go-1" {
		t.Fatalf("GetScheduleHistory failed: history=%+v err=%v", history, err)
	}
	delRes, err := client.DeleteSchedule(ctx, "sched-go-1")
	if err != nil || !delRes.Success {
		t.Fatalf("DeleteSchedule failed: delRes=%+v err=%v", delRes, err)
	}

	multi, err := client.DownloadMulti(ctx, "https://example.com/multi", []string{"text", "markdown"}, nil)
	if err != nil || multi["url"] != "https://example.com/multi" {
		t.Fatalf("DownloadMulti failed: multi=%+v err=%v", multi, err)
	}
	_, err = client.DownloadMulti(ctx, "https://example.com/multi", []string{"text"}, nil)
	var multiErr *APIError
	if !errors.As(err, &multiErr) || multiErr.ErrorType != "unexpected_content_type" {
		t.Fatalf("expected unexpected_content_type APIError, got %v", err)
	}

	_, err = client.Download(ctx, "https://blocked.local", nil)
	var apiErr *APIError
	if !errors.As(err, &apiErr) || apiErr.StatusCode != 400 || apiErr.ErrorType != "validation_error" {
		t.Fatalf("expected 400 validation_error APIError, got %v", err)
	}
}

func TestDownloadEncodesTargetQueryAndFragment(t *testing.T) {
	var gotRequestURI string
	var gotDecodedPath string
	var gotRenderParam string

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotRequestURI = r.RequestURI
		gotDecodedPath = r.URL.Path
		gotRenderParam = r.URL.Query().Get("render")
		w.Header().Set("Content-Type", "text/plain")
		_, _ = w.Write([]byte("query-preserved"))
	}))
	defer srv.Close()

	client := New(srv.URL, "go-secret")
	target := "https://example.com/search?q=hello%20world&page=2#sec"
	res, err := client.Download(context.Background(), target, &DownloadOptions{Render: true})
	if err != nil {
		t.Fatalf("Download failed: %v", err)
	}
	if res.Text() != "query-preserved" {
		t.Fatalf("unexpected body: %q", res.Text())
	}
	wantURI := "/https://example.com/search%3Fq=hello%2520world&page=2%23sec?render=true"
	if gotRequestURI != wantURI {
		t.Fatalf("expected RequestURI %q, got %q", wantURI, gotRequestURI)
	}
	if gotDecodedPath != "/https://example.com/search?q=hello%20world&page=2#sec" {
		t.Fatalf("unexpected decoded URL.Path: %q", gotDecodedPath)
	}
	if gotRenderParam != "true" {
		t.Fatalf("expected render=true query param, got %q", gotRenderParam)
	}
}
