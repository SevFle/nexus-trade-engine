import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";
import {
  ApiClient,
  ApiError,
  defaultTokenGetter,
  DEFAULT_TOKEN_STORAGE_KEY,
  type ApiClientOptions,
} from "../api";

// ---------------------------------------------------------------------------
// Mock fetch helpers
// ---------------------------------------------------------------------------

type FetchCall = {
  url: string;
  init?: RequestInit;
};

/** Build a minimal `Response`-like object for `fetch` to resolve with. */
function mockResponse(
  status: number,
  body: unknown,
  headers: Record<string, string> = {},
): Response {
  const jsonHeaders = { "content-type": "application/json", ...headers };
  const payload =
    typeof body === "string" || body === null ? body : JSON.stringify(body);
  return {
    status,
    ok: status >= 200 && status < 300,
    headers: new Headers(jsonHeaders),
    text: () => Promise.resolve(payload === null ? "" : String(payload)),
  } as unknown as Response;
}

/** Create a client whose `fetch` is `vi.fn()` and return the call recorder. */
function clientWithSpy(
  response: Response,
  options: ApiClientOptions = {},
): { client: ApiClient; fetchMock: ReturnType<typeof vi.fn>; calls: FetchCall[] } {
  const calls: FetchCall[] = [];
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    return response;
  });
  const client = new ApiClient({
    baseUrl: "https://api.test",
    fetchImpl: fetchMock as unknown as typeof fetch,
    ...options,
  });
  return { client, fetchMock, calls };
}

describe("ApiClient", () => {
  let originalFetch: typeof fetch;

  beforeEach(() => {
    originalFetch = globalThis.fetch;
    localStorage.clear();
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  // -------------------------------------------------------------------------
  // Construction & URL building
  // -------------------------------------------------------------------------

  describe("construction", () => {
    it("strips trailing slashes from the configured base URL", async () => {
      const { client, calls } = clientWithSpy(mockResponse(200, { ok: true }), {
        baseUrl: "https://api.test/",
      });
      await client.get("/api/v1/strategies/");
      expect(calls[0].url).toBe("https://api.test/api/v1/strategies/");
    });

    it("joins base + path even when path has a leading slash", async () => {
      const { client, calls } = clientWithSpy(mockResponse(200, {}));
      await client.get("/foo/bar");
      expect(calls[0].url).toBe("https://api.test/foo/bar");
    });

    it("allows absolute URLs as the path to bypass the base", async () => {
      const { client, calls } = clientWithSpy(mockResponse(200, {}));
      await client.get("https://other.test/x");
      expect(calls[0].url).toBe("https://other.test/x");
    });

    it("serializes query params and skips null/undefined values", async () => {
      const { client, calls } = clientWithSpy(mockResponse(200, {}));
      await client.get("/p", {
        query: { a: 1, b: "two", c: null, d: undefined, e: false, f: ["x", "y"] },
      });
      const qs = new URL(calls[0].url).search;
      const params = new URLSearchParams(qs);
      expect(params.get("a")).toBe("1");
      expect(params.get("b")).toBe("two");
      expect(params.get("c")).toBeNull();
      expect(params.get("d")).toBeNull();
      expect(params.get("e")).toBe("false");
      expect(params.getAll("f")).toEqual(["x", "y"]);
    });

    it("includes credentials: include on every request", async () => {
      const { client, calls } = clientWithSpy(mockResponse(200, {}));
      await client.get("/x");
      expect(calls[0].init?.credentials).toBe("include");
    });
  });

  // -------------------------------------------------------------------------
  // Token injection
  // -------------------------------------------------------------------------

  describe("token injection", () => {
    it("injects Authorization: Bearer <token> when localStorage has a token", async () => {
      localStorage.setItem(DEFAULT_TOKEN_STORAGE_KEY, "jwt-abc-123");
      const { client, calls } = clientWithSpy(mockResponse(200, {}));
      await client.get("/api/v1/strategies/");
      const headers = new Headers(calls[0].init?.headers);
      expect(headers.get("authorization")).toBe("Bearer jwt-abc-123");
    });

    it("omits the Authorization header entirely when no token is present", async () => {
      const { client, calls } = clientWithSpy(mockResponse(200, {}));
      await client.get("/api/v1/strategies/");
      const headers = new Headers(calls[0].init?.headers);
      expect(headers.get("authorization")).toBeNull();
    });

    it("omits the Authorization header when the token getter returns null", async () => {
      const { client, calls } = clientWithSpy(mockResponse(200, {}), {
        tokenGetter: () => null,
      });
      await client.get("/x");
      const headers = new Headers(calls[0].init?.headers);
      expect(headers.get("authorization")).toBeNull();
    });

    it("omits the Authorization header when the token getter returns an empty string", async () => {
      const { client, calls } = clientWithSpy(mockResponse(200, {}), {
        tokenGetter: () => "",
      });
      await client.get("/x");
      const headers = new Headers(calls[0].init?.headers);
      expect(headers.get("authorization")).toBeNull();
    });

    it("uses a custom sync token getter", async () => {
      const { client, calls } = clientWithSpy(mockResponse(200, {}), {
        tokenGetter: () => "custom-token",
      });
      await client.get("/x");
      const headers = new Headers(calls[0].init?.headers);
      expect(headers.get("authorization")).toBe("Bearer custom-token");
    });

    it("supports an async token getter (e.g. auth context with refresh)", async () => {
      const { client, calls } = clientWithSpy(mockResponse(200, {}), {
        tokenGetter: async () => "async-token",
      });
      await client.get("/x");
      const headers = new Headers(calls[0].init?.headers);
      expect(headers.get("authorization")).toBe("Bearer async-token");
    });

    it("defaultTokenGetter reads localStorage and tolerates failures", () => {
      localStorage.setItem(DEFAULT_TOKEN_STORAGE_KEY, "stored-jwt");
      expect(defaultTokenGetter()).toBe("stored-jwt");
    });

    it("defaultTokenGetter fails closed (returns null) when localStorage throws", () => {
      const spy = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
        throw new Error("private mode");
      });
      expect(defaultTokenGetter()).toBeNull();
      spy.mockRestore();
    });

    it("per-request headers can override the Authorization header", async () => {
      localStorage.setItem(DEFAULT_TOKEN_STORAGE_KEY, "stored");
      const { client, calls } = clientWithSpy(mockResponse(200, {}));
      await client.get("/x", { headers: { Authorization: "Token override" } });
      const headers = new Headers(calls[0].init?.headers);
      expect(headers.get("authorization")).toBe("Token override");
    });

    it("does not re-add Content-Type for GET requests", async () => {
      const { client, calls } = clientWithSpy(mockResponse(200, {}));
      await client.get("/x");
      const headers = new Headers(calls[0].init?.headers);
      expect(headers.get("content-type")).toBeNull();
      expect(headers.get("accept")).toBe("application/json");
    });

    it("applies defaultHeaders to every request", async () => {
      const { client, calls } = clientWithSpy(mockResponse(200, {}), {
        defaultHeaders: { "X-Client": "nexus-web" },
      });
      await client.get("/x");
      const headers = new Headers(calls[0].init?.headers);
      expect(headers.get("x-client")).toBe("nexus-web");
    });

    it("per-request headers override defaultHeaders", async () => {
      const { client, calls } = clientWithSpy(mockResponse(200, {}), {
        defaultHeaders: { "X-Client": "default" },
      });
      await client.get("/x", { headers: { "X-Client": "override" } });
      const headers = new Headers(calls[0].init?.headers);
      expect(headers.get("x-client")).toBe("override");
    });
  });

  // -------------------------------------------------------------------------
  // Request body serialization
  // -------------------------------------------------------------------------

  describe("body serialization", () => {
    it("JSON-serializes object bodies and sets Content-Type", async () => {
      const { client, calls } = clientWithSpy(mockResponse(200, {}));
      await client.post("/p", { name: "alpha", initial_capital: 1000 });
      const headers = new Headers(calls[0].init?.headers);
      expect(headers.get("content-type")).toBe("application/json");
      expect(calls[0].init?.body).toBe(
        JSON.stringify({ name: "alpha", initial_capital: 1000 }),
      );
      expect(calls[0].init?.method).toBe("POST");
    });

    it("does not override an explicitly-provided Content-Type", async () => {
      const { client, calls } = clientWithSpy(mockResponse(200, {}));
      await client.post("/p", { x: 1 }, { headers: { "Content-Type": "application/vnd.custom+json" } });
      const headers = new Headers(calls[0].init?.headers);
      expect(headers.get("content-type")).toBe("application/vnd.custom+json");
    });

    it("passes string bodies through verbatim without forcing Content-Type", async () => {
      const { client, calls } = clientWithSpy(mockResponse(200, {}));
      await client.post("/p", "raw-text-body");
      const headers = new Headers(calls[0].init?.headers);
      expect(headers.get("content-type")).toBeNull();
      expect(calls[0].init?.body).toBe("raw-text-body");
    });

    it("does not send a body for undefined/null", async () => {
      const { client, calls } = clientWithSpy(mockResponse(200, {}));
      await client.post("/p", undefined);
      expect(calls[0].init?.body).toBeUndefined();
    });

    it("forwards FormData bodies untouched", async () => {
      const { client, calls } = clientWithSpy(mockResponse(200, {}));
      const fd = new FormData();
      fd.append("file", "blob");
      await client.post("/upload", fd);
      expect(calls[0].init?.body).toBe(fd);
      const headers = new Headers(calls[0].init?.headers);
      expect(headers.get("content-type")).toBeNull();
    });
  });

  // -------------------------------------------------------------------------
  // Response parsing
  // -------------------------------------------------------------------------

  describe("response parsing", () => {
    it("parses a JSON 200 response into the typed shape", async () => {
      const payload = { id: "u1", name: "Main", description: "", initial_capital: 1000, created_at: "2026-01-01T00:00:00" };
      const { client } = clientWithSpy(mockResponse(200, payload));
      const result = await client.get("/api/v1/portfolio/x");
      expect(result).toEqual(payload);
    });

    it("returns null for HTTP 204 with no body", async () => {
      const { client } = clientWithSpy(mockResponse(204, null));
      const result = await client.put("/p", {});
      expect(result).toBeNull();
    });

    it("parses arrays as the top-level body", async () => {
      const list = [
        { id: "1", name: "A", description: "", initial_capital: 1, created_at: "t" },
        { id: "2", name: "B", description: "", initial_capital: 2, created_at: "t" },
      ];
      const { client } = clientWithSpy(mockResponse(200, list));
      const result = await client.get("/api/v1/portfolio/");
      expect(result).toEqual(list);
      expect(Array.isArray(result)).toBe(true);
    });

    it("best-effort parses JSON even when content-type is missing", async () => {
      const { client } = clientWithSpy(
        mockResponse(200, { ok: true }, { "content-type": "text/plain" }),
      );
      const result = await client.get("/x");
      expect(result).toEqual({ ok: true });
    });

    it("falls back to the raw text when the body is genuinely non-JSON", async () => {
      const { client } = clientWithSpy(
        mockResponse(200, "<html>not json</html>", { "content-type": "text/html" }),
      );
      const result = await client.get("/x");
      expect(result).toBe("<html>not json</html>");
    });

    it("returns null when the response body is empty (but status is 200)", async () => {
      const { client } = clientWithSpy(mockResponse(200, ""));
      const result = await client.get("/x");
      expect(result).toBeNull();
    });
  });

  // -------------------------------------------------------------------------
  // Error handling
  // -------------------------------------------------------------------------

  describe("error handling", () => {
    it("throws ApiError with status and parsed body on a 404", async () => {
      const { client } = clientWithSpy(
        mockResponse(404, { detail: "Strategy 'foo' not found" }),
      );
      await expect(client.get("/api/v1/strategies/foo")).rejects.toMatchObject({
        name: "ApiError",
        status: 404,
        message: "Strategy 'foo' not found",
        body: { detail: "Strategy 'foo' not found" },
        isClientError: true,
        isServerError: false,
        isNetworkError: false,
      });
    });

    it("surfaces FastAPI detail string as the message on 500", async () => {
      const { client } = clientWithSpy(
        mockResponse(500, { detail: "Failed to activate: boom" }),
      );
      await expect(client.post("/api/v1/strategies/x/activate", {})).rejects.toMatchObject({
        status: 500,
        message: "Failed to activate: boom",
        isServerError: true,
      });
    });

    it("uses a generic fallback message when there is no detail", async () => {
      const { client } = clientWithSpy(mockResponse(502, {}));
      await expect(client.get("/x")).rejects.toMatchObject({
        status: 502,
        message: "Request failed with 502",
        isServerError: true,
      });
    });

    it("uses the plain-text body as the message when no detail object exists", async () => {
      const { client } = clientWithSpy(
        mockResponse(400, "Bad Request", { "content-type": "text/plain" }),
      );
      await expect(client.get("/x")).rejects.toMatchObject({
        status: 400,
        message: "Bad Request",
      });
    });

    it("records method and url on the error", async () => {
      const { client } = clientWithSpy(mockResponse(403, { detail: "Access denied" }));
      let caught: ApiError | undefined;
      try {
        await client.get("/api/v1/portfolio/zzz");
      } catch (e) {
        caught = e as ApiError;
      }
      expect(caught).toBeInstanceOf(ApiError);
      expect(caught!.method).toBe("GET");
      expect(caught!.url).toBe("https://api.test/api/v1/portfolio/zzz");
    });

    it("wraps transport-level fetch failures as a network error (status 0)", async () => {
      const calls: FetchCall[] = [];
      const fetchMock = vi.fn(async (url: string) => {
        calls.push({ url });
        throw new TypeError("Failed to fetch");
      });
      const client = new ApiClient({
        baseUrl: "https://api.test",
        fetchImpl: fetchMock as unknown as typeof fetch,
      });
      await expect(client.get("/x")).rejects.toMatchObject({
        name: "ApiError",
        status: 0,
        message: "Failed to fetch",
        isNetworkError: true,
        isClientError: false,
        isServerError: false,
      });
    });

    it("wraps a non-Error fetch rejection", async () => {
      const fetchMock = vi.fn(async () => {
        throw "string reason";
      });
      const client = new ApiClient({
        baseUrl: "https://api.test",
        fetchImpl: fetchMock as unknown as typeof fetch,
      });
      await expect(client.get("/x")).rejects.toMatchObject({
        status: 0,
        message: "string reason",
        isNetworkError: true,
      });
    });

    it("reports auth errors (401/403) via isAuthError", async () => {
      const { client: c401 } = clientWithSpy(mockResponse(401, { detail: "Not authenticated" }));
      await expect(c401.get("/x")).rejects.toMatchObject({ status: 401, isAuthError: true });
      const { client: c403 } = clientWithSpy(mockResponse(403, { detail: "Forbidden" }));
      await expect(c403.get("/x")).rejects.toMatchObject({ status: 403, isAuthError: true });
    });

    it("reports HTTP 451 as consent-required", async () => {
      const documents = [{ id: "tos-v2", version: "2.0" }];
      const { client } = clientWithSpy(
        mockResponse(451, { detail: { documents } }),
      );
      let caught: ApiError | undefined;
      try {
        await client.get("/api/v1/strategies/");
      } catch (e) {
        caught = e as ApiError;
      }
      expect(caught).toBeInstanceOf(ApiError);
      expect(caught!.isConsentRequired).toBe(true);
      expect((caught!.body as { detail: { documents: unknown[] } }).detail.documents).toEqual(documents);
    });

    it("ApiError is a real Error with a stack and name", async () => {
      const { client } = clientWithSpy(mockResponse(500, { detail: "boom" }));
      let caught: ApiError | undefined;
      try {
        await client.get("/x");
      } catch (e) {
        caught = e as ApiError;
      }
      expect(caught).toBeInstanceOf(Error);
      expect(caught!.name).toBe("ApiError");
      expect(typeof caught!.stack).toBe("string");
    });
  });

  // -------------------------------------------------------------------------
  // Resource methods — strategies / portfolios / backtests
  // -------------------------------------------------------------------------

  describe("resource methods", () => {
    it("listStrategies() calls GET /api/v1/strategies/ and returns the typed list", async () => {
      const payload = {
        strategies: [{ id: "sma", name: "SMA Cross", is_loaded: true }],
      };
      const { client, calls } = clientWithSpy(mockResponse(200, payload));
      const result = await client.listStrategies();
      expect(result.strategies).toHaveLength(1);
      expect(result.strategies[0].id).toBe("sma");
      expect(calls[0].url).toBe("https://api.test/api/v1/strategies/");
      expect(calls[0].init?.method).toBe("GET");
    });

    it("getStrategy() encodes the strategy id into the path", async () => {
      const detail = {
        id: "momentum",
        name: "Momentum",
        version: "1.2.0",
        author: "nexus",
        description: "trend follower",
        config_schema: {},
        data_feeds: ["ohlcvd"],
        watchlist: ["AAPL"],
        requires_network: false,
        requires_gpu: false,
        is_loaded: true,
      };
      const { client, calls } = clientWithSpy(mockResponse(200, detail));
      const result = await client.getStrategy("momentum");
      expect(result).toEqual(detail);
      expect(calls[0].url).toBe("https://api.test/api/v1/strategies/momentum");
    });

    it("activateStrategy() posts params and returns the action response", async () => {
      const { client, calls } = clientWithSpy(
        mockResponse(200, { status: "activated", strategy_id: "sma", name: "SMA", version: "1.0" }),
      );
      const result = await client.activateStrategy("sma", { period: 14 });
      expect(result.status).toBe("activated");
      expect(JSON.parse(String(calls[0].init?.body))).toEqual({ params: { period: 14 } });
      expect(calls[0].url).toBe("https://api.test/api/v1/strategies/sma/activate");
    });

    it("URL-encodes ids that contain reserved characters", async () => {
      const { client, calls } = clientWithSpy(mockResponse(200, {}));
      await client.getStrategy("a/b c");
      expect(calls[0].url).toBe("https://api.test/api/v1/strategies/a%2Fb%20c");
    });

    it("createPortfolio() posts the request body", async () => {
      const created = {
        id: "p1",
        name: "Growth",
        description: "desc",
        initial_capital: 50000,
        created_at: "2026-01-01T00:00:00",
      };
      const { client, calls } = clientWithSpy(mockResponse(200, created));
      const result = await client.createPortfolio({ name: "Growth", initial_capital: 50000 });
      expect(result).toEqual(created);
      expect(calls[0].url).toBe("https://api.test/api/v1/portfolio/");
      expect(calls[0].init?.method).toBe("POST");
      expect(JSON.parse(String(calls[0].init?.body))).toEqual({
        name: "Growth",
        initial_capital: 50000,
      });
    });

    it("listPortfolios() returns a typed array", async () => {
      const list = [
        { id: "p1", name: "A", description: "", initial_capital: 1, created_at: "t1" },
      ];
      const { client } = clientWithSpy(mockResponse(200, list));
      const result = await client.listPortfolios();
      expect(Array.isArray(result)).toBe(true);
      expect(result[0].id).toBe("p1");
    });

    it("deletePortfolio() returns the {status,id} body", async () => {
      const { client, calls } = clientWithSpy(
        mockResponse(200, { status: "deleted", id: "p1" }),
      );
      const result = await client.deletePortfolio("p1");
      expect(result).toEqual({ status: "deleted", id: "p1" });
      expect(calls[0].init?.method).toBe("DELETE");
      expect(calls[0].url).toBe("https://api.test/api/v1/portfolio/p1");
    });

    it("submitBacktest() posts to /api/v1/backtest and returns the id", async () => {
      const { client, calls } = clientWithSpy(
        mockResponse(202, { status: "accepted", backtest_id: "bk-1" }),
      );
      const result = await client.submitBacktest({
        strategy_name: "sma",
        symbol: "AAPL",
        start_date: "2024-01-01",
        end_date: "2024-12-31",
        initial_capital: "100000",
      });
      expect(result).toEqual({ status: "accepted", backtest_id: "bk-1" });
      expect(calls[0].url).toBe("https://api.test/api/v1/backtest");
      expect(calls[0].init?.method).toBe("POST");
    });

    it("runBacktest() posts to the synchronous /run endpoint", async () => {
      const { client, calls } = clientWithSpy(
        mockResponse(200, { status: "accepted", backtest_id: "bk-2" }),
      );
      await client.runBacktest({
        strategy_name: "sma",
        symbol: "AAPL",
        start_date: "2024-01-01",
        end_date: "2024-12-31",
      });
      expect(calls[0].url).toBe("https://api.test/api/v1/backtest/run");
    });

    it("getBacktestResult() parses the full result including nested metrics", async () => {
      const payload = {
        status: "completed",
        strategy_name: "sma",
        symbol: "AAPL",
        initial_capital: 100000,
        final_value: 110000,
        metrics: {
          total_return_pct: 10,
          annualized_return_pct: 10,
          sharpe_ratio: 1.5,
          sortino_ratio: 2.0,
          max_drawdown_pct: 5,
          max_drawdown_duration_days: 30,
          max_drawdown_recovery_days: 20,
          calmar_ratio: 2,
          volatility_annual_pct: 7,
          total_trades: 42,
          win_rate: 0.55,
          profit_factor: 1.8,
          avg_trade_pnl: 100,
          avg_winner: 200,
          avg_loser: -150,
          best_trade: 500,
          worst_trade: -300,
          max_consecutive_wins: 5,
          max_consecutive_losses: 3,
          total_costs: 250,
          total_taxes: 100,
          cost_drag_pct: 0.25,
          turnover_ratio: 1.2,
          exposure_pct: 80,
          rolling_metrics: [
            {
              window_days: 30,
              sharpe_ratio: 1.1,
              sortino_ratio: 1.2,
              volatility_annual_pct: 6,
              max_drawdown_pct: 3,
            },
          ],
        },
        equity_curve: [{ date: "2024-01-01", value: 100000 }],
        drawdown_curve: [0, -2, -5],
        error: null,
      };
      const { client, calls } = clientWithSpy(mockResponse(200, payload));
      const result = await client.getBacktestResult("bk-1");
      expect(result.status).toBe("completed");
      expect(result.metrics.sharpe_ratio).toBe(1.5);
      expect(result.metrics.rolling_metrics[0].window_days).toBe(30);
      expect(result.drawdown_curve).toEqual([0, -2, -5]);
      expect(result.equity_curve[0].value).toBe(100000);
      expect(calls[0].url).toBe("https://api.test/api/v1/backtest/results/bk-1");
    });
  });

  // -------------------------------------------------------------------------
  // Global fetch fallback
  // -------------------------------------------------------------------------

  describe("global fetch fallback", () => {
    it("uses globalThis.fetch when no fetchImpl is provided", async () => {
      const fake = vi.fn(async () => mockResponse(200, { ok: true }));
      globalThis.fetch = fake as unknown as typeof fetch;
      const client = new ApiClient({ baseUrl: "https://api.test" });
      const result = await client.get("/x");
      expect(result).toEqual({ ok: true });
      expect(fake).toHaveBeenCalledTimes(1);
      const callUrl = fake.mock.calls[0][0] as string;
      expect(callUrl).toBe("https://api.test/x");
    });
  });
});
