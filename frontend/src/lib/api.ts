/**
 * Typed HTTP client for the NexusTrade backend API.
 *
 * This module provides a single {@link ApiClient} class that wraps `fetch`
 * with automatic JWT injection, typed response interfaces, and structured
 * error handling. It is deliberately framework-agnostic — there are no
 * React/hooks here so the client can be reused (and unit-tested) in
 * isolation. Components consume it via React Query or a thin wrapper.
 *
 * The response interfaces mirror the key backend routes:
 *   - strategies : /api/v1/strategies
 *   - portfolios  : /api/v1/portfolio
 *   - backtests   : /api/v1/backtest
 *
 * Source of truth for shapes: engine/api/routes/{strategies,portfolio,backtest}.py
 */

// ---------------------------------------------------------------------------
// Token provider
// ---------------------------------------------------------------------------

/**
 * A function that resolves the current access token. It may be async so it can
 * transparently wrap an auth context, a token-refresh promise, or plain
 * storage. Returning `null` means "no credentials" — the request is sent
 * unauthenticated.
 */
export type TokenGetter = () => string | null | Promise<string | null>;

/** Default storage key used by {@link defaultTokenGetter}. */
export const DEFAULT_TOKEN_STORAGE_KEY = "nexus_access_token";

/**
 * Default token source: reads the JWT from `localStorage`.
 *
 * `localStorage` access is wrapped in try/catch because it throws in private
 * browsing mode, sandboxed iframes, and during SSR — in all those cases we
 * fail *closed* (treat as unauthenticated) rather than crashing the call.
 */
export function defaultTokenGetter(): string | null {
  try {
    return localStorage.getItem(DEFAULT_TOKEN_STORAGE_KEY);
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// HTTP primitives
// ---------------------------------------------------------------------------

export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

/** Query-string values supported by {@link ApiClient.request}. */
export type QueryValue = string | number | boolean | null | undefined;

/** Options accepted by {@link ApiClient.request}. */
export interface RequestOptions {
  method?: HttpMethod;
  /** Request body — objects are JSON-serialized, strings/FormData pass through. */
  body?: unknown;
  headers?: Record<string, string>;
  query?: Record<string, QueryValue | QueryValue[]>;
  signal?: AbortSignal;
}

export interface ApiClientOptions {
  /** Base URL (no trailing slash). Defaults to `VITE_API_URL` or localhost. */
  baseUrl?: string;
  /** Token source. Defaults to {@link defaultTokenGetter}. */
  tokenGetter?: TokenGetter;
  /** Headers applied to every request. Per-request headers win. */
  defaultHeaders?: Record<string, string>;
  /**
   * Injectable fetch implementation. Defaults to `globalThis.fetch`.
   * Exposed primarily for unit testing without monkey-patching globals.
   */
  fetchImpl?: typeof fetch;
}

// ---------------------------------------------------------------------------
// Response interfaces — strategies
// ---------------------------------------------------------------------------

/** Shape returned for each entry of `GET /api/v1/strategies/`. */
export interface StrategySummary {
  id: string;
  name: string;
  version?: string;
  author?: string;
  description?: string;
  is_loaded?: boolean;
  /**
   * Optional runtime status token from the engine (e.g. "active",
   * "idle", "paused", "error"). When absent the UI derives a status from
   * {@link is_loaded}. Kept optional because older engine builds do not
   * emit it in the list response.
   */
  status?: string;
  /** Optional realised/unrealised P&L for the strategy, in account currency. */
  pnl?: number;
  /** Optional P&L expressed as a percentage (e.g. 1.11 for 1.11%). */
  pnl_pct?: number;
}

/** Response of `GET /api/v1/strategies/`. */
export interface StrategyListResponse {
  strategies: StrategySummary[];
}

/** Response of `GET /api/v1/strategies/{id}`. */
export interface StrategyDetail {
  id: string;
  name: string;
  version: string;
  author: string;
  description: string;
  config_schema: unknown;
  data_feeds: string[];
  watchlist: string[];
  requires_network: boolean;
  requires_gpu: boolean;
  is_loaded: boolean;
}

/** Response of activate / deactivate / reload actions. */
export interface StrategyActionResponse {
  status: string;
  strategy_id: string;
  name?: string;
  version?: string;
}

/** Response of `GET /api/v1/strategies/{id}/health`. */
export interface StrategyHealthResponse {
  strategy_id: string;
  is_loaded: boolean;
}

// ---------------------------------------------------------------------------
// Response interfaces — portfolios
// ---------------------------------------------------------------------------

/** `PortfolioResponse` from engine/api/routes/portfolio.py. */
export interface Portfolio {
  id: string;
  name: string;
  description: string;
  initial_capital: number;
  created_at: string;
}

/**
 * `PortfolioSummaryResponse` from `GET /api/v1/portfolio/summary`.
 *
 * `total_pnl` / `total_pnl_pct` are unrealised P&L (the engine has no
 * intraday baseline yet); the dashboard overview card surfaces them as the
 * P&L summary. `as_of` is an ISO-8601 UTC timestamp.
 */
export interface PortfolioSummaryData {
  total_value: number;
  total_pnl: number;
  total_pnl_pct: number;
  active_strategies: number;
  open_positions: number;
  currency: string;
  as_of: string;
}

/** Body of `POST /api/v1/portfolio/`. */
export interface CreatePortfolioRequest {
  name: string;
  description?: string;
  initial_capital?: number;
}

/** Response of `DELETE /api/v1/portfolio/{id}`. */
export interface PortfolioActionResponse {
  status: string;
  id: string;
}

// ---------------------------------------------------------------------------
// Response interfaces — backtests
// ---------------------------------------------------------------------------

/**
 * Body of `POST /api/v1/backtest` and `POST /api/v1/backtest/run`.
 *
 * `initial_capital` is transmitted as a string so the engine can parse it
 * with its high-precision decimal type (e.g. Python `Decimal`) without any
 * intermediate IEEE-754 rounding — important for large/notional capitals
 * whose float representation is lossy. The frontend validates the string
 * with a strict decimal regex before submission.
 */
export interface BacktestSubmitRequest {
  strategy_name: string;
  symbol: string;
  start_date: string;
  end_date: string;
  initial_capital?: string;
  config?: Record<string, unknown>;
}

/** 202 response of the backtest submission endpoints. */
export interface BacktestSubmitResponse {
  status: string;
  backtest_id: string;
}

/** Rolling window snapshot inside {@link BacktestMetrics}. */
export interface RollingMetricsSnapshot {
  window_days: number;
  sharpe_ratio: number;
  sortino_ratio: number | null;
  volatility_annual_pct: number;
  max_drawdown_pct: number;
}

/** Full metrics block of a completed backtest. */
export interface BacktestMetrics {
  total_return_pct: number;
  annualized_return_pct: number;
  sharpe_ratio: number;
  sortino_ratio: number | null;
  max_drawdown_pct: number;
  max_drawdown_duration_days: number;
  max_drawdown_recovery_days: number | null;
  calmar_ratio: number | null;
  volatility_annual_pct: number;
  total_trades: number;
  win_rate: number;
  profit_factor: number | null;
  avg_trade_pnl: number;
  avg_winner: number;
  avg_loser: number;
  best_trade: number;
  worst_trade: number;
  max_consecutive_wins: number;
  max_consecutive_losses: number;
  total_costs: number;
  total_taxes: number;
  cost_drag_pct: number;
  turnover_ratio: number;
  exposure_pct: number;
  rolling_metrics: RollingMetricsSnapshot[];
}

/** Response of `GET /api/v1/backtest/results/{id}`. */
export interface BacktestResult {
  status: string;
  strategy_name: string;
  symbol: string;
  initial_capital: number;
  final_value: number;
  metrics: BacktestMetrics;
  equity_curve: Array<Record<string, unknown>>;
  drawdown_curve: number[];
  error: string | null;
  evaluation?: Record<string, unknown> | null;
}

// ---------------------------------------------------------------------------
// Structured error handling
// ---------------------------------------------------------------------------

/**
 * Error raised for any non-2xx response or transport failure.
 *
 * `status` is the HTTP status code. A transport-level failure (network down,
 * DNS error, CORS rejection) has no status, so it is reported as `0`.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;
  readonly url: string;
  readonly method: HttpMethod;

  constructor(
    message: string,
    status: number,
    body: unknown,
    url: string,
    method: HttpMethod,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
    this.url = url;
    this.method = method;
  }

  /** HTTP 5xx — safe to retry. */
  get isServerError(): boolean {
    return this.status >= 500 && this.status < 600;
  }

  /** HTTP 4xx — client-side problem, retrying unchanged won't help. */
  get isClientError(): boolean {
    return this.status >= 400 && this.status < 500;
  }

  /** Transport failure (no status code at all). */
  get isNetworkError(): boolean {
    return this.status === 0;
  }

  /** True for 401/403 — credentials missing or insufficient. */
  get isAuthError(): boolean {
    return this.status === 401 || this.status === 403;
  }

  /**
   * Legal consent required. The backend returns HTTP 451 with a
   * `{detail: {documents: [...]}}` body when a protected route is hit
   * before the user has accepted the latest legal documents.
   */
  get isConsentRequired(): boolean {
    return this.status === 451;
  }
}

// ---------------------------------------------------------------------------
// Client
// ---------------------------------------------------------------------------

function defaultBaseUrl(): string {
  // import.meta.env is provided by Vite; fall back to localhost for tests
  // and local dev where the variable may be unset.
  const fromEnv =
    (import.meta as unknown as { env?: Record<string, string> }).env
      ?.VITE_API_URL;
  return fromEnv || "http://localhost:8000";
}

/** Join a base URL and a path, normalizing any duplicate slashes. */
function joinUrl(base: string, path: string): string {
  if (/^https?:\/\//i.test(path)) return path;
  const left = base.replace(/\/+$/, "");
  const right = path.replace(/^\/+/, "");
  return `${left}/${right}`;
}

/** Serialize a query map into a `?a=b&c=d` string. */
function buildQueryString(query: Record<string, QueryValue | QueryValue[]>): string {
  const params = new URLSearchParams();
  for (const [key, raw] of Object.entries(query)) {
    const values = Array.isArray(raw) ? raw : [raw];
    for (const v of values) {
      if (v === null || v === undefined) continue;
      params.append(key, String(v));
    }
  }
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}

/**
 * Extract a human-readable message from a parsed error body.
 * FastAPI `HTTPException` bodies look like `{"detail": "..."}`.
 */
function messageFromBody(status: number, body: unknown, fallback: string): string {
  if (body && typeof body === "object") {
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail.length > 0) return detail;
    // Some validation errors use a list detail; surface a compact hint.
    if (Array.isArray(detail) && detail.length > 0) {
      return fallback;
    }
  }
  if (typeof body === "string" && body.length > 0) return body;
  return fallback;
}

export class ApiClient {
  private readonly baseUrl: string;
  private readonly tokenGetter: TokenGetter;
  private readonly defaultHeaders: Record<string, string>;
  private readonly fetchImpl: typeof fetch;

  constructor(options: ApiClientOptions = {}) {
    this.baseUrl = (options.baseUrl ?? defaultBaseUrl()).replace(/\/+$/, "");
    this.tokenGetter = options.tokenGetter ?? defaultTokenGetter;
    this.defaultHeaders = { ...options.defaultHeaders };
    this.fetchImpl = options.fetchImpl ?? globalThis.fetch.bind(globalThis);
  }

  // -- core -----------------------------------------------------------------

  /**
   * Perform an HTTP request and return the parsed JSON body, or `null` for
   * HTTP 204. Throws {@link ApiError} on any non-2xx status or transport
   * failure.
   *
   * - JWT is injected as `Authorization: Bearer <token>` when the token
   *   getter returns a non-empty value.
   * - Request bodies that are plain objects are JSON-serialized and
   *   `Content-Type: application/json` is set automatically.
   */
  async request<T>(path: string, options: RequestOptions = {}): Promise<T> {
    const method: HttpMethod = options.method ?? "GET";
    const url = joinUrl(this.baseUrl, path) + buildQueryString(options.query ?? {});

    const headers: Record<string, string> = {
      Accept: "application/json",
      ...this.defaultHeaders,
      ...options.headers,
    };

    // Token injection. Awaited so async token getters (e.g. refresh-aware
    // context) work transparently.
    const token = await this.tokenGetter();
    if (token) {
      headers.Authorization = `Bearer ${token}`;
    }

    let body: BodyInit | undefined;
    if (options.body !== undefined && options.body !== null) {
      if (
        typeof options.body === "string" ||
        options.body instanceof FormData ||
        options.body instanceof URLSearchParams ||
        options.body instanceof Blob ||
        options.body instanceof ArrayBuffer
      ) {
        body = options.body as BodyInit;
      } else {
        headers["Content-Type"] = headers["Content-Type"] ?? "application/json";
        body = JSON.stringify(options.body);
      }
    }

    let response: Response;
    try {
      response = await this.fetchImpl(url, {
        method,
        headers,
        body,
        signal: options.signal,
        credentials: "include",
      });
    } catch (err) {
      // fetch rejects on network errors / CORS / DNS — wrap uniformly.
      const reason = err instanceof Error ? err.message : String(err);
      throw new ApiError(
        reason || "Network request failed",
        0,
        null,
        url,
        method,
      );
    }

    if (response.status === 204) {
      return null as unknown as T;
    }

    // Parse the body once. Try JSON first; fall back to text; finally null.
    let parsed: unknown = null;
    const text = await response.text();
    if (text.length > 0) {
      const ct = response.headers.get("content-type") ?? "";
      if (ct.includes("application/json")) {
        try {
          parsed = JSON.parse(text);
        } catch {
          parsed = text;
        }
      } else {
        // Many error responses are JSON without the correct content-type;
        // attempt a best-effort parse before giving up.
        try {
          parsed = JSON.parse(text);
        } catch {
          parsed = text;
        }
      }
    }

    if (!response.ok) {
      const message = messageFromBody(
        response.status,
        parsed,
        `Request failed with ${response.status}`,
      );
      throw new ApiError(message, response.status, parsed, url, method);
    }

    return parsed as T;
  }

  // -- verb helpers ---------------------------------------------------------

  get<T>(path: string, options: Omit<RequestOptions, "method" | "body"> = {}): Promise<T> {
    return this.request<T>(path, { ...options, method: "GET" });
  }

  post<T>(path: string, body?: unknown, options: Omit<RequestOptions, "method"> = {}): Promise<T> {
    return this.request<T>(path, { ...options, method: "POST", body });
  }

  put<T>(path: string, body?: unknown, options: Omit<RequestOptions, "method"> = {}): Promise<T> {
    return this.request<T>(path, { ...options, method: "PUT", body });
  }

  patch<T>(path: string, body?: unknown, options: Omit<RequestOptions, "method"> = {}): Promise<T> {
    return this.request<T>(path, { ...options, method: "PATCH", body });
  }

  delete<T>(path: string, options: Omit<RequestOptions, "method" | "body"> = {}): Promise<T> {
    return this.request<T>(path, { ...options, method: "DELETE" });
  }

  // -- strategies -----------------------------------------------------------

  listStrategies(): Promise<StrategyListResponse> {
    return this.get<StrategyListResponse>("/api/v1/strategies/");
  }

  getStrategy(strategyId: string): Promise<StrategyDetail> {
    return this.get<StrategyDetail>(`/api/v1/strategies/${encodeURIComponent(strategyId)}`);
  }

  activateStrategy(strategyId: string, params: Record<string, unknown> = {}): Promise<StrategyActionResponse> {
    return this.post<StrategyActionResponse>(
      `/api/v1/strategies/${encodeURIComponent(strategyId)}/activate`,
      { params },
    );
  }

  deactivateStrategy(strategyId: string): Promise<StrategyActionResponse> {
    return this.post<StrategyActionResponse>(
      `/api/v1/strategies/${encodeURIComponent(strategyId)}/deactivate`,
    );
  }

  reloadStrategy(strategyId: string): Promise<StrategyActionResponse> {
    return this.post<StrategyActionResponse>(
      `/api/v1/strategies/${encodeURIComponent(strategyId)}/reload`,
    );
  }

  getStrategyHealth(strategyId: string): Promise<StrategyHealthResponse> {
    return this.get<StrategyHealthResponse>(
      `/api/v1/strategies/${encodeURIComponent(strategyId)}/health`,
    );
  }

  // -- portfolios -----------------------------------------------------------

  listPortfolios(): Promise<Portfolio[]> {
    return this.get<Portfolio[]>("/api/v1/portfolio/");
  }

  createPortfolio(req: CreatePortfolioRequest): Promise<Portfolio> {
    return this.post<Portfolio>("/api/v1/portfolio/", req);
  }

  getPortfolio(portfolioId: string): Promise<Portfolio> {
    return this.get<Portfolio>(`/api/v1/portfolio/${encodeURIComponent(portfolioId)}`);
  }

  /** Aggregate overview for the dashboard: `GET /api/v1/portfolio/summary`. */
  getPortfolioSummary(): Promise<PortfolioSummaryData> {
    return this.get<PortfolioSummaryData>("/api/v1/portfolio/summary");
  }

  deletePortfolio(portfolioId: string): Promise<PortfolioActionResponse> {
    return this.delete<PortfolioActionResponse>(
      `/api/v1/portfolio/${encodeURIComponent(portfolioId)}`,
    );
  }

  // -- backtests ------------------------------------------------------------

  /**
   * Submit a backtest via `POST /api/v1/backtest` (202 Accepted).
   * The run happens asynchronously; poll {@link getBacktestResult}.
   */
  submitBacktest(req: BacktestSubmitRequest): Promise<BacktestSubmitResponse> {
    return this.post<BacktestSubmitResponse>("/api/v1/backtest", req);
  }

  /** Synchronous backtest kick-off via `POST /api/v1/backtest/run`. */
  runBacktest(req: BacktestSubmitRequest): Promise<BacktestSubmitResponse> {
    return this.post<BacktestSubmitResponse>("/api/v1/backtest/run", req);
  }

  /** Fetch the result/status of a backtest by id. */
  getBacktestResult(backtestId: string): Promise<BacktestResult> {
    return this.get<BacktestResult>(
      `/api/v1/backtest/results/${encodeURIComponent(backtestId)}`,
    );
  }
}

// ---------------------------------------------------------------------------
// Shared singleton
// ---------------------------------------------------------------------------

/**
 * Process-wide singleton client used by React components and hooks. It reads
 * the API base URL from `VITE_API_URL` and the JWT from `localStorage` via
 * {@link defaultTokenGetter}. Construct a dedicated {@link ApiClient} instance
 * (e.g. with an injectable `fetchImpl`) for tests or non-default token sources.
 */
export const apiClient = new ApiClient();
