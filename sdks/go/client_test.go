package downloaderclient

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
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
			_, _ = w.Write([]byte(`{"status":"healthy","version":"0.7.0","environment":"test","draining":false}`))
		case "/render":
			var body RenderRequest
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Fatalf("decode render body: %v", err)
			}
			if body.Output != "markdown" || len(body.Actions) != 1 {
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
			_, _ = w.Write([]byte(`{"job_id":"job-go-1","status":"completed","progress":100,"total_urls":1,"processed_urls":1,"successful_urls":1,"failed_urls":0,"results_available":true}`))
		case "/jobs/job-go-1/results":
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"job_id":"job-go-1","status":"completed","total_duration":0.3,"results":[{"url":"https://example.com","success":true}],"summary":{"successful_requests":1},"created_at":"2026-11-12T00:00:00Z","completed_at":"2026-11-12T00:00:01Z"}`))
		case "/schedules":
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"id":"sched-go-1","name":"Hourly","url":"https://example.com","cron_expression":"0 * * * *","format":"screenshot","enabled":true,"created_at":"2026-11-12T00:00:00Z"}`))
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
	if health.Version != "0.7.0" {
		t.Fatalf("expected version 0.7.0, got %s", health.Version)
	}

	shot, err := client.Screenshot(ctx, "https://example.com/shot", true, "png", nil)
	if err != nil {
		t.Fatalf("Screenshot failed: %v", err)
	}
	if len(shot.Body) != 4 || shot.Body[0] != 0x89 {
		t.Fatalf("unexpected screenshot bytes: %v", shot.Body)
	}

	rendered, err := client.Render(ctx, RenderRequest{
		URL:     "https://example.com",
		Output:  "markdown",
		Actions: []BrowserAction{{Type: "click", Selector: "#go"}},
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
	if err != nil || !st.ResultsAvailable {
		t.Fatalf("GetJobStatus failed: st=%+v err=%v", st, err)
	}
	res, err := client.GetJobResults(ctx, sub.JobID)
	if err != nil || len(res.Results) != 1 {
		t.Fatalf("GetJobResults failed: res=%+v err=%v", res, err)
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

	_, err = client.Download(ctx, "https://blocked.local", nil)
	var apiErr *APIError
	if !errors.As(err, &apiErr) || apiErr.StatusCode != 400 || apiErr.ErrorType != "validation_error" {
		t.Fatalf("expected 400 validation_error APIError, got %v", err)
	}
}
