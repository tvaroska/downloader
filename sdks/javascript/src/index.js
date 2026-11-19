// @ts-check
/**
 * Typed zero-dependency client for the REST API Downloader service.
 */

/** @type {Record<string, string>} */
const FORMAT_TO_ACCEPT = {
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

/**
 * Percent-encode `%`, `?`, and `#` so target query/fragment survive `GET /{url:path}`.
 * @param {string} url
 * @returns {string}
 */
export function encodeTargetUrlPath(url) {
  return url
    .replace(/^\/+/, "")
    .replace(/%/g, "%25")
    .replace(/\?/g, "%3F")
    .replace(/#/g, "%23");
}

/**
 * Validate and percent-encode a single URL path segment.
 * @param {string} id
 * @param {string} field
 * @returns {string}
 */
export function encodePathSegment(id, field) {
  if (!id || id === "." || id === "..") {
    throw new Error(`${field} must be a non-empty path segment and not '.' or '..'`);
  }
  return encodeURIComponent(id);
}

export class DownloaderApiError extends Error {
  /**
   * @param {string} message
   * @param {number | null} statusCode
   * @param {string | null} [errorType]
   * @param {number | null} [retryAfter]
   */
  constructor(message, statusCode, errorType = null, retryAfter = null) {
    super(message);
    this.name = "DownloaderApiError";
    /** @type {number | null} */
    this.statusCode = statusCode;
    /** @type {string | null} */
    this.errorType = errorType;
    /** @type {number | null} */
    this.retryAfter = retryAfter;
  }
}

export class DownloaderClient {
  /**
   * @param {import("./index.js").DownloaderClientOptions} options
   */
  constructor(options) {
    /** @private @type {string} */
    this.baseUrl = options.baseUrl.replace(/\/+$/, "");
    /** @private @type {string | undefined} */
    this.apiKey = options.apiKey;
    /** @private @type {typeof globalThis.fetch} */
    this.fetchImpl = options.fetch ?? globalThis.fetch;
    /** @private @type {Record<string, string>} */
    this.defaultHeaders = { ...(options.defaultHeaders ?? {}) };
  }

  /**
   * @private
   * @param {Record<string, string>} [extra]
   * @returns {Record<string, string>}
   */
  headers(extra = {}) {
    /** @type {Record<string, string>} */
    const out = { ...this.defaultHeaders, ...extra };
    if (this.apiKey) {
      out["X-API-Key"] = this.apiKey;
    }
    return out;
  }

  /**
   * @private
   * @param {Response} resp
   * @returns {Promise<never>}
   */
  async handleError(resp) {
    let message = `HTTP ${resp.status}`;
    /** @type {string | null} */
    let errorType = null;
    try {
      const data = /** @type {Record<string, unknown>} */ (await resp.json());
      const detail = /** @type {Record<string, unknown> | string} */ (data.detail ?? data);
      if (typeof detail === "string") {
        message = detail;
      } else if (detail && typeof detail === "object") {
        message = String(detail.error ?? message);
        errorType = detail.error_type ? String(detail.error_type) : null;
      }
    } catch {
      // Retain HTTP status message when response body is non-JSON.
    }
    const retryRaw = resp.headers.get("retry-after");
    const retryAfter = retryRaw && /^\d+$/.test(retryRaw) ? Number.parseInt(retryRaw, 10) : null;
    throw new DownloaderApiError(message, resp.status, errorType, retryAfter);
  }

  /**
   * @private
   * @param {string} url
   * @param {Response} resp
   * @returns {Promise<import("./index.js").DownloadResult>}
   */
  async toDownloadResult(url, resp) {
    if (!resp.ok) {
      await this.handleError(resp);
    }
    const buf = new Uint8Array(await resp.arrayBuffer());
    /** @type {Record<string, string>} */
    const headers = {};
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

  /**
   * @param {AbortSignal} [signal]
   * @returns {Promise<import("./index.js").HealthStatus>}
   */
  async health(signal) {
    const resp = await this.fetchImpl(`${this.baseUrl}/health`, {
      method: "GET",
      headers: this.headers(),
      signal,
    });
    if (!resp.ok) {
      await this.handleError(resp);
    }
    return /** @type {import("./index.js").HealthStatus} */ (await resp.json());
  }

  /**
   * @private
   * @param {string} url
   * @param {import("./index.js").DownloadOptions} [options]
   * @returns {string}
   */
  buildDownloadUrl(url, options = {}) {
    const targetPath = encodeTargetUrlPath(url);
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
    return `${this.baseUrl}/${targetPath}${qs ? `?${qs}` : ""}`;
  }

  /**
   * @param {string} url
   * @param {import("./index.js").DownloadOptions} [options]
   * @returns {Promise<import("./index.js").DownloadResult>}
   */
  async download(url, options = {}) {
    const fullUrl = this.buildDownloadUrl(url, options);
    const accept = FORMAT_TO_ACCEPT[options.format ?? "text"] ?? options.format ?? "text/plain";
    const resp = await this.fetchImpl(fullUrl, {
      method: "GET",
      headers: this.headers({ Accept: accept }),
      signal: options.signal,
    });
    return this.toDownloadResult(url, resp);
  }

  /**
   * @param {string} url
   * @param {Array<import("./index.js").OutputFormat | string>} formats
   * @param {Omit<import("./index.js").DownloadOptions, "output" | "format">} [options]
   * @returns {Promise<Record<string, unknown>>}
   */
  async downloadMulti(url, formats, options = {}) {
    const fullUrl = this.buildDownloadUrl(url, options);
    const accept = formats.map((f) => FORMAT_TO_ACCEPT[f] ?? f).join(", ");
    const resp = await this.fetchImpl(fullUrl, {
      method: "GET",
      headers: this.headers({ Accept: accept }),
      signal: options.signal,
    });
    if (!resp.ok) {
      await this.handleError(resp);
    }
    const contentType = (resp.headers.get("content-type") ?? "").split(";")[0].trim().toLowerCase();
    if (contentType && contentType !== "application/json" && !contentType.endsWith("+json")) {
      throw new DownloaderApiError(
        `Expected JSON multi-format response, got ${contentType}`,
        resp.status,
        "unexpected_content_type",
      );
    }
    try {
      const data = await resp.json();
      if (!data || typeof data !== "object" || Array.isArray(data)) {
        throw new DownloaderApiError(
          "Expected JSON object in multi-format response",
          resp.status,
          "invalid_json_response",
        );
      }
      return /** @type {Record<string, unknown>} */ (data);
    } catch (err) {
      if (err instanceof DownloaderApiError) {
        throw err;
      }
      throw new DownloaderApiError(
        `Expected JSON multi-format response, got invalid JSON (${contentType || "unknown"})`,
        resp.status,
        "invalid_json_response",
      );
    }
  }

  /**
   * @param {string} url
   * @param {Omit<import("./index.js").DownloadOptions, "output" | "format">} [options]
   * @returns {Promise<import("./index.js").DownloadResult>}
   */
  async screenshot(url, options = {}) {
    return this.download(url, { ...options, output: "screenshot" });
  }

  /**
   * @param {string} url
   * @param {Omit<import("./index.js").DownloadOptions, "output" | "format">} [options]
   * @returns {Promise<import("./index.js").DownloadResult>}
   */
  async pdf(url, options = {}) {
    return this.download(url, { ...options, output: "pdf" });
  }

  /**
   * @param {string} imageUrl
   * @param {{ format?: "text" | "markdown"; language?: string; signal?: AbortSignal }} [options]
   * @returns {Promise<import("./index.js").DownloadResult>}
   */
  async ocr(imageUrl, options = {}) {
    return this.download(imageUrl, {
      format: options.format ?? "text",
      ocr: true,
      ocrLang: options.language,
      signal: options.signal,
    });
  }

  /**
   * @param {string} url
   * @param {import("./index.js").RenderOptions} [options]
   * @returns {Promise<import("./index.js").DownloadResult>}
   */
  async render(url, options = {}) {
    /** @type {Record<string, unknown>} */
    const payload = {
      url,
      output: options.output ?? "html",
      wait_for: options.waitFor ?? null,
      actions: options.actions ?? [],
      full_page: options.fullPage ?? false,
      screenshot_format: options.screenshotFormat ?? "png",
    };
    if (options.timeoutMs !== undefined) {
      payload.timeout_ms = options.timeoutMs;
    }
    const resp = await this.fetchImpl(`${this.baseUrl}/render`, {
      method: "POST",
      headers: this.headers({ "Content-Type": "application/json" }),
      body: JSON.stringify(payload),
      signal: options.signal,
    });
    return this.toDownloadResult(url, resp);
  }

  /**
   * @param {Array<string | import("./index.js").BatchURLItem>} urls
   * @param {import("./index.js").BatchOptions} [options]
   * @returns {Promise<import("./index.js").BatchJobSubmission>}
   */
  async submitBatch(urls, options = {}) {
    /** @type {Record<string, unknown>} */
    const payload = {
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
    return /** @type {import("./index.js").BatchJobSubmission} */ (await resp.json());
  }

  /**
   * @param {string} jobId
   * @param {AbortSignal} [signal]
   * @returns {Promise<import("./index.js").BatchJobStatus>}
   */
  async getJobStatus(jobId, signal) {
    const encoded = encodePathSegment(jobId, "jobId");
    const resp = await this.fetchImpl(`${this.baseUrl}/jobs/${encoded}/status`, {
      method: "GET",
      headers: this.headers(),
      signal,
    });
    if (!resp.ok) {
      await this.handleError(resp);
    }
    return /** @type {import("./index.js").BatchJobStatus} */ (await resp.json());
  }

  /**
   * @param {string} jobId
   * @param {AbortSignal} [signal]
   * @returns {Promise<import("./index.js").BatchJobResults>}
   */
  async getJobResults(jobId, signal) {
    const encoded = encodePathSegment(jobId, "jobId");
    const resp = await this.fetchImpl(`${this.baseUrl}/jobs/${encoded}/results`, {
      method: "GET",
      headers: this.headers(),
      signal,
    });
    if (!resp.ok) {
      await this.handleError(resp);
    }
    return /** @type {import("./index.js").BatchJobResults} */ (await resp.json());
  }

  /**
   * @param {string} jobId
   * @param {AbortSignal} [signal]
   * @returns {Promise<import("./index.js").JobCancelResult>}
   */
  async cancelJob(jobId, signal) {
    const encoded = encodePathSegment(jobId, "jobId");
    const resp = await this.fetchImpl(`${this.baseUrl}/jobs/${encoded}`, {
      method: "DELETE",
      headers: this.headers(),
      signal,
    });
    if (!resp.ok) {
      await this.handleError(resp);
    }
    return /** @type {import("./index.js").JobCancelResult} */ (await resp.json());
  }

  /**
   * @param {string} jobId
   * @param {{ pollIntervalMs?: number; timeoutMs?: number; signal?: AbortSignal }} [options]
   * @returns {Promise<import("./index.js").BatchJobResults>}
   */
  async waitForBatch(jobId, options = {}) {
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
          null,
          `batch_${status.status}`,
        );
      }
      if (Date.now() >= deadline) {
        throw new Error(`Timed out waiting for batch job ${jobId}`);
      }
      await new Promise((resolve) => setTimeout(resolve, pollMs));
    }
  }

  /**
   * @param {import("./index.js").ScheduleCreateRequest} schedule
   * @param {AbortSignal} [signal]
   * @returns {Promise<import("./index.js").ScheduleInfo>}
   */
  async createSchedule(schedule, signal) {
    const resp = await this.fetchImpl(`${this.baseUrl}/schedules`, {
      method: "POST",
      headers: this.headers({ "Content-Type": "application/json" }),
      body: JSON.stringify(schedule),
      signal,
    });
    if (!resp.ok) {
      await this.handleError(resp);
    }
    return /** @type {import("./index.js").ScheduleInfo} */ (await resp.json());
  }

  /**
   * @param {AbortSignal} [signal]
   * @returns {Promise<import("./index.js").ScheduleInfo[]>}
   */
  async listSchedules(signal) {
    const resp = await this.fetchImpl(`${this.baseUrl}/schedules`, {
      method: "GET",
      headers: this.headers(),
      signal,
    });
    if (!resp.ok) {
      await this.handleError(resp);
    }
    const data = /** @type {{ schedules?: import("./index.js").ScheduleInfo[] }} */ (
      await resp.json()
    );
    return data.schedules ?? [];
  }

  /**
   * @param {string} scheduleId
   * @param {AbortSignal} [signal]
   * @returns {Promise<import("./index.js").ScheduleInfo>}
   */
  async getSchedule(scheduleId, signal) {
    const encoded = encodePathSegment(scheduleId, "scheduleId");
    const resp = await this.fetchImpl(`${this.baseUrl}/schedules/${encoded}`, {
      method: "GET",
      headers: this.headers(),
      signal,
    });
    if (!resp.ok) {
      await this.handleError(resp);
    }
    return /** @type {import("./index.js").ScheduleInfo} */ (await resp.json());
  }

  /**
   * @param {string} scheduleId
   * @param {{ limit?: number; offset?: number; signal?: AbortSignal }} [options]
   * @returns {Promise<import("./index.js").ScheduleHistory>}
   */
  async getScheduleHistory(scheduleId, options = {}) {
    const encoded = encodePathSegment(scheduleId, "scheduleId");
    const params = new URLSearchParams();
    params.set("limit", String(options.limit ?? 20));
    params.set("offset", String(options.offset ?? 0));
    const resp = await this.fetchImpl(
      `${this.baseUrl}/schedules/${encoded}/history?${params.toString()}`,
      {
        method: "GET",
        headers: this.headers(),
        signal: options.signal,
      },
    );
    if (!resp.ok) {
      await this.handleError(resp);
    }
    return /** @type {import("./index.js").ScheduleHistory} */ (await resp.json());
  }

  /**
   * @param {string} scheduleId
   * @param {AbortSignal} [signal]
   * @returns {Promise<{ success: boolean }>}
   */
  async deleteSchedule(scheduleId, signal) {
    const encoded = encodePathSegment(scheduleId, "scheduleId");
    const resp = await this.fetchImpl(`${this.baseUrl}/schedules/${encoded}`, {
      method: "DELETE",
      headers: this.headers(),
      signal,
    });
    if (!resp.ok) {
      await this.handleError(resp);
    }
    return /** @type {{ success: boolean }} */ (await resp.json());
  }
}
