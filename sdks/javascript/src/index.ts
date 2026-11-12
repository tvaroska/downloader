/**
 * Typed zero-dependency client for the REST API Downloader v0.7.0+ service.
 */

export type BrowserActionType =
  | "click"
  | "fill"
  | "select"
  | "press"
  | "hover"
  | "scroll"
  | "wait_for_selector"
  | "wait";

export interface BrowserAction {
  type: BrowserActionType;
  selector?: string;
  value?: string;
  key?: string;
  x?: number;
  y?: number;
  timeout_ms?: number;
  duration_ms?: number;
}

export type OutputFormat =
  | "text"
  | "markdown"
  | "html"
  | "pdf"
  | "screenshot"
  | "screenshot_jpeg"
  | "json"
  | "raw";

export interface DownloadOptions {
  format?: OutputFormat;
  render?: boolean;
  waitFor?: string;
  output?: "screenshot" | "pdf";
  fullPage?: boolean;
  screenshotFormat?: "png" | "jpeg";
  actions?: BrowserAction[];
  ocr?: boolean;
  ocrLang?: string;
  signal?: AbortSignal;
}

export interface DownloadResult {
  url: string;
  statusCode: number;
  contentType: string;
  bytes: Uint8Array;
  headers: Record<string, string>;
  text(): string;
  readonly renderedWithJs: boolean;
  readonly ocrApplied: boolean;
  readonly traceId: string | null;
}

export interface RenderOptions {
  output?: "html" | "text" | "markdown" | "pdf" | "screenshot";
  waitFor?: string;
  actions?: BrowserAction[];
  fullPage?: boolean;
  screenshotFormat?: "png" | "jpeg";
  signal?: AbortSignal;
}

export interface BatchURLItem {
  url: string;
  format?: string;
  custom_headers?: Record<string, string>;
  actions?: BrowserAction[];
  full_page?: boolean;
  screenshot_format?: "png" | "jpeg";
  ocr_language?: string;
}

export interface BatchOptions {
  defaultFormat?: string;
  concurrencyLimit?: number;
  timeoutPerUrl?: number;
  webhookUrl?: string;
  webhookSecret?: string;
  signal?: AbortSignal;
}

export interface BatchJobSubmission {
  job_id: string;
  status: string;
  created_at: string;
  total_urls: number;
  estimated_completion?: string | null;
}

export interface BatchJobStatus {
  job_id: string;
  status: string;
  progress: number;
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  total_urls: number;
  processed_urls: number;
  successful_urls: number;
  failed_urls: number;
  error_message?: string | null;
  results_available: boolean;
  expires_at?: string | null;
}

export interface BatchJobResults {
  job_id: string;
  status: string;
  total_duration: number;
  results: Array<Record<string, unknown>>;
  summary: Record<string, unknown>;
  created_at: string;
  completed_at: string;
}

export interface ScheduleCreateRequest {
  name: string;
  url: string;
  cron_expression: string;
  format?: "text" | "html" | "markdown" | "pdf" | "screenshot" | "json" | "raw";
  headers?: Record<string, string>;
  enabled?: boolean;
  webhook_url?: string;
  webhook_secret?: string;
}

export interface ScheduleInfo {
  id: string;
  name: string;
  url: string;
  cron_expression: string;
  format: string;
  headers?: Record<string, string> | null;
  enabled: boolean;
  created_at: string;
  next_run_time?: string | null;
  webhook_url?: string | null;
}

export interface HealthStatus {
  status: string;
  version: string;
  environment: string;
  draining?: boolean;
  active_requests?: number;
  deployment_mode?: string;
  telemetry_enabled?: boolean;
  configuration?: Record<string, unknown>;
  services?: Record<string, unknown>;
}

export interface DownloaderClientOptions {
  baseUrl: string;
  apiKey?: string;
  fetch?: typeof globalThis.fetch;
  defaultHeaders?: Record<string, string>;
}

export class DownloaderApiError extends Error {
  readonly statusCode: number;
  readonly errorType: string | null;
  readonly retryAfter: number | null;

  constructor(
    message: string,
    statusCode: number,
    errorType: string | null = null,
    retryAfter: number | null = null,
  ) {
    super(message);
    this.name = "DownloaderApiError";
    this.statusCode = statusCode;
    this.errorType = errorType;
    this.retryAfter = retryAfter;
  }
}

const FORMAT_TO_ACCEPT: Record<string, string> = {
  text: "text/plain",
  markdown: "text/markdown",
  html: "text/html",
  pdf: "application/pdf",
  screenshot: "image/png",
  screenshot_jpeg: "image/jpeg",
  json: "application/json",
  raw: "*/*",
};

const decoder = new TextDecoder();

export class DownloaderClient {
  private readonly baseUrl: string;
  private readonly apiKey?: string;
  private readonly fetchImpl: typeof globalThis.fetch;
  private readonly defaultHeaders: Record<string, string>;

  constructor(options: DownloaderClientOptions) {
    this.baseUrl = options.baseUrl.replace(/\/+$/, "");
    this.apiKey = options.apiKey;
    this.fetchImpl = options.fetch ?? globalThis.fetch;
    this.defaultHeaders = { ...(options.defaultHeaders ?? {}) };
  }

  private headers(extra: Record<string, string> = {}): Record<string, string> {
    const out: Record<string, string> = { ...this.defaultHeaders, ...extra };
    if (this.apiKey) {
      out["X-API-Key"] = this.apiKey;
    }
    return out;
  }

  private async handleError(resp: Response): Promise<never> {
    let message = `HTTP ${resp.status}`;
    let errorType: string | null = null;
    try {
      const data = (await resp.json()) as Record<string, unknown>;
      const detail = (data.detail ?? data) as Record<string, unknown> | string;
      if (typeof detail === "string") {
        message = detail;
      } else if (detail && typeof detail === "object") {
        message = String(detail.error ?? message);
        errorType = detail.error_type ? String(detail.error_type) : null;
      }
    } catch {
      // Ignore body parse errors and retain HTTP status message.
    }
    const retryRaw = resp.headers.get("retry-after");
    const retryAfter = retryRaw && /^\d+$/.test(retryRaw) ? Number.parseInt(retryRaw, 10) : null;
    throw new DownloaderApiError(message, resp.status, errorType, retryAfter);
  }

  private async toDownloadResult(url: string, resp: Response): Promise<DownloadResult> {
    if (!resp.ok) {
      await this.handleError(resp);
    }
    const buf = new Uint8Array(await resp.arrayBuffer());
    const headers: Record<string, string> = {};
    resp.headers.forEach((value, key) => {
      headers[key.toLowerCase()] = value;
    });
    return {
      url,
      statusCode: resp.status,
      contentType: headers["content-type"] ?? "application/octet-stream",
      bytes: buf,
      headers,
      text: () => decoder.decode(buf),
      renderedWithJs: (headers["x-rendered-with-js"] ?? "").toLowerCase() === "true",
      ocrApplied: (headers["x-ocr-applied"] ?? "").toLowerCase() === "true",
      traceId: headers["x-trace-id"] ?? null,
    };
  }

  async health(signal?: AbortSignal): Promise<HealthStatus> {
    const resp = await this.fetchImpl(`${this.baseUrl}/health`, {
      method: "GET",
      headers: this.headers(),
      signal,
    });
    if (!resp.ok) {
      await this.handleError(resp);
    }
    return (await resp.json()) as HealthStatus;
  }

  async download(url: string, options: DownloadOptions = {}): Promise<DownloadResult> {
    const targetPath = url.replace(/^\/+/, "");
    const params = new URLSearchParams();
    if (options.render) params.set("render", "true");
    if (options.waitFor) params.set("wait_for", options.waitFor);
    if (options.output) params.set("output", options.output);
    if (options.fullPage) params.set("full_page", "true");
    if (options.screenshotFormat && options.screenshotFormat !== "png") {
      params.set("screenshot_format", options.screenshotFormat);
    }
    if (options.actions && options.actions.length > 0) {
      params.set("actions", JSON.stringify(options.actions));
    }
    if (options.ocr) params.set("ocr", "true");
    if (options.ocrLang) params.set("ocr_lang", options.ocrLang);

    const qs = params.toString();
    const fullUrl = `${this.baseUrl}/${targetPath}${qs ? `?${qs}` : ""}`;
    const accept = FORMAT_TO_ACCEPT[options.format ?? "text"] ?? options.format ?? "text/plain";
    const resp = await this.fetchImpl(fullUrl, {
      method: "GET",
      headers: this.headers({ Accept: accept }),
      signal: options.signal,
    });
    return this.toDownloadResult(url, resp);
  }

  async screenshot(
    url: string,
    options: Omit<DownloadOptions, "output" | "format"> = {},
  ): Promise<DownloadResult> {
    return this.download(url, { ...options, output: "screenshot" });
  }

  async pdf(
    url: string,
    options: Omit<DownloadOptions, "output" | "format"> = {},
  ): Promise<DownloadResult> {
    return this.download(url, { ...options, output: "pdf" });
  }

  async ocr(
    imageUrl: string,
    options: { format?: "text" | "markdown"; language?: string; signal?: AbortSignal } = {},
  ): Promise<DownloadResult> {
    return this.download(imageUrl, {
      format: options.format ?? "text",
      ocr: true,
      ocrLang: options.language,
      signal: options.signal,
    });
  }

  async render(url: string, options: RenderOptions = {}): Promise<DownloadResult> {
    const resp = await this.fetchImpl(`${this.baseUrl}/render`, {
      method: "POST",
      headers: this.headers({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        url,
        output: options.output ?? "html",
        wait_for: options.waitFor ?? null,
        actions: options.actions ?? [],
        full_page: options.fullPage ?? false,
        screenshot_format: options.screenshotFormat ?? "png",
      }),
      signal: options.signal,
    });
    return this.toDownloadResult(url, resp);
  }

  async submitBatch(
    urls: Array<string | BatchURLItem>,
    options: BatchOptions = {},
  ): Promise<BatchJobSubmission> {
    const payload: Record<string, unknown> = {
      urls: urls.map((u) => (typeof u === "string" ? { url: u } : u)),
      default_format: options.defaultFormat ?? "text",
    };
    if (options.concurrencyLimit !== undefined) {
      payload.concurrency_limit = options.concurrencyLimit;
    }
    if (options.timeoutPerUrl !== undefined) {
      payload.timeout_per_url = options.timeoutPerUrl;
    }
    if (options.webhookUrl !== undefined) {
      payload.webhook_url = options.webhookUrl;
    }
    if (options.webhookSecret !== undefined) {
      payload.webhook_secret = options.webhookSecret;
    }
    const resp = await this.fetchImpl(`${this.baseUrl}/batch`, {
      method: "POST",
      headers: this.headers({ "Content-Type": "application/json" }),
      body: JSON.stringify(payload),
      signal: options.signal,
    });
    if (!resp.ok) {
      await this.handleError(resp);
    }
    return (await resp.json()) as BatchJobSubmission;
  }

  async getJobStatus(jobId: string, signal?: AbortSignal): Promise<BatchJobStatus> {
    const resp = await this.fetchImpl(`${this.baseUrl}/jobs/${encodeURIComponent(jobId)}/status`, {
      method: "GET",
      headers: this.headers(),
      signal,
    });
    if (!resp.ok) {
      await this.handleError(resp);
    }
    return (await resp.json()) as BatchJobStatus;
  }

  async getJobResults(jobId: string, signal?: AbortSignal): Promise<BatchJobResults> {
    const resp = await this.fetchImpl(
      `${this.baseUrl}/jobs/${encodeURIComponent(jobId)}/results`,
      {
        method: "GET",
        headers: this.headers(),
        signal,
      },
    );
    if (!resp.ok) {
      await this.handleError(resp);
    }
    return (await resp.json()) as BatchJobResults;
  }

  async waitForBatch(
    jobId: string,
    options: { pollIntervalMs?: number; timeoutMs?: number; signal?: AbortSignal } = {},
  ): Promise<BatchJobResults> {
    const pollMs = options.pollIntervalMs ?? 1000;
    const deadline = Date.now() + (options.timeoutMs ?? 120_000);
    while (true) {
      const status = await this.getJobStatus(jobId, options.signal);
      if (status.status === "completed") {
        return this.getJobResults(jobId, options.signal);
      }
      if (status.status === "failed" || status.status === "cancelled") {
        throw new DownloaderApiError(
          status.error_message ?? `Batch job ${jobId} ${status.status}`,
          500,
          `batch_${status.status}`,
        );
      }
      if (Date.now() >= deadline) {
        throw new Error(`Timed out waiting for batch job ${jobId}`);
      }
      await new Promise((resolve) => setTimeout(resolve, pollMs));
    }
  }

  async createSchedule(
    schedule: ScheduleCreateRequest,
    signal?: AbortSignal,
  ): Promise<ScheduleInfo> {
    const resp = await this.fetchImpl(`${this.baseUrl}/schedules`, {
      method: "POST",
      headers: this.headers({ "Content-Type": "application/json" }),
      body: JSON.stringify(schedule),
      signal,
    });
    if (!resp.ok) {
      await this.handleError(resp);
    }
    return (await resp.json()) as ScheduleInfo;
  }

  async listSchedules(signal?: AbortSignal): Promise<ScheduleInfo[]> {
    const resp = await this.fetchImpl(`${this.baseUrl}/schedules`, {
      method: "GET",
      headers: this.headers(),
      signal,
    });
    if (!resp.ok) {
      await this.handleError(resp);
    }
    const data = (await resp.json()) as { schedules?: ScheduleInfo[] };
    return data.schedules ?? [];
  }

  async deleteSchedule(scheduleId: string, signal?: AbortSignal): Promise<{ success: boolean }> {
    const resp = await this.fetchImpl(`${this.baseUrl}/schedules/${encodeURIComponent(scheduleId)}`, {
      method: "DELETE",
      headers: this.headers(),
      signal,
    });
    if (!resp.ok) {
      await this.handleError(resp);
    }
    return (await resp.json()) as { success: boolean };
  }
}
