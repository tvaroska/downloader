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
  timeoutMs?: number;
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

export interface ScheduleExecution {
  execution_id: string;
  schedule_id: string;
  status: string;
  started_at: string;
  completed_at?: string | null;
  duration?: number | null;
  success: boolean;
  content_size?: number | null;
  error_message?: string | null;
  attempt: number;
}

export interface ScheduleHistory {
  executions: ScheduleExecution[];
  total: number;
  limit: number;
  offset: number;
}

export interface JobCancelResult {
  success: boolean;
  message: string;
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

export declare function encodeTargetUrlPath(url: string): string;
export declare function encodePathSegment(id: string, field: string): string;

export declare class DownloaderApiError extends Error {
  readonly statusCode: number | null;
  readonly errorType: string | null;
  readonly retryAfter: number | null;
  constructor(
    message: string,
    statusCode: number | null,
    errorType?: string | null,
    retryAfter?: number | null,
  );
}

export declare class DownloaderClient {
  constructor(options: DownloaderClientOptions);
  health(signal?: AbortSignal): Promise<HealthStatus>;
  download(url: string, options?: DownloadOptions): Promise<DownloadResult>;
  downloadMulti(
    url: string,
    formats: Array<OutputFormat | string>,
    options?: Omit<DownloadOptions, "output" | "format">,
  ): Promise<Record<string, unknown>>;
  screenshot(
    url: string,
    options?: Omit<DownloadOptions, "output" | "format">,
  ): Promise<DownloadResult>;
  pdf(url: string, options?: Omit<DownloadOptions, "output" | "format">): Promise<DownloadResult>;
  ocr(
    imageUrl: string,
    options?: { format?: "text" | "markdown"; language?: string; signal?: AbortSignal },
  ): Promise<DownloadResult>;
  render(url: string, options?: RenderOptions): Promise<DownloadResult>;
  submitBatch(
    urls: Array<string | BatchURLItem>,
    options?: BatchOptions,
  ): Promise<BatchJobSubmission>;
  getJobStatus(jobId: string, signal?: AbortSignal): Promise<BatchJobStatus>;
  getJobResults(jobId: string, signal?: AbortSignal): Promise<BatchJobResults>;
  cancelJob(jobId: string, signal?: AbortSignal): Promise<JobCancelResult>;
  waitForBatch(
    jobId: string,
    options?: { pollIntervalMs?: number; timeoutMs?: number; signal?: AbortSignal },
  ): Promise<BatchJobResults>;
  createSchedule(schedule: ScheduleCreateRequest, signal?: AbortSignal): Promise<ScheduleInfo>;
  listSchedules(signal?: AbortSignal): Promise<ScheduleInfo[]>;
  getSchedule(scheduleId: string, signal?: AbortSignal): Promise<ScheduleInfo>;
  getScheduleHistory(
    scheduleId: string,
    options?: { limit?: number; offset?: number; signal?: AbortSignal },
  ): Promise<ScheduleHistory>;
  deleteSchedule(scheduleId: string, signal?: AbortSignal): Promise<{ success: boolean }>;
}
