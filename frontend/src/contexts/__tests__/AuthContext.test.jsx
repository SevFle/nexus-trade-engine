import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { AuthProvider, useAuth } from "../AuthContext";
import * as authApi from "../../api/auth";

vi.mock("../../api/auth", () => ({
  login: vi.fn(),
  register: vi.fn(),
  refreshToken: vi.fn(),
  fetchMe: vi.fn(),
  logout: vi.fn(),
  setAccessToken: vi.fn(),
  clearAccessToken: vi.fn(),
  handleOAuthCallback: vi.fn(),
  fetchOAuthAuthorizeUrl: vi.fn(),
}));

function createJwt(payload, expSecondsFromNow = 3600) {
  const header = btoa(JSON.stringify({ alg: "HS256", typ: "JWT" }));
  const exp = Math.floor(Date.now() / 1000) + expSecondsFromNow;
  const body = btoa(JSON.stringify({ ...payload, exp }));
  return `${header}.${body}.signature`;
}

function wrapper({ children }) {
  return <AuthProvider>{children}</AuthProvider>;
}

describe("AuthContext — M1: OAuth flow fix", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("startOAuth fetches authorize URL then redirects", async () => {
    const fakeUrl = "https://accounts.google.com/o/oauth2/v2/auth?scope=openid";
    authApi.fetchOAuthAuthorizeUrl.mockResolvedValue(fakeUrl);

    const originalHref = window.location.href;
    const locationAssign = vi.fn();
    Object.defineProperty(window, "location", {
      value: { href: originalHref, assign: locationAssign },
      writable: true,
      configurable: true,
    });

    const { result } = renderHook(() => useAuth(), { wrapper });

    await act(async () => {
      await result.current.startOAuth("google");
    });

    expect(authApi.fetchOAuthAuthorizeUrl).toHaveBeenCalledWith("google");
    expect(window.location.href).toBe(fakeUrl);
  });

  it("startOAuth does not navigate if fetch fails", async () => {
    authApi.fetchOAuthAuthorizeUrl.mockRejectedValue(new Error("Network error"));

    const { result } = renderHook(() => useAuth(), { wrapper });
    const originalHref = window.location.href;

    await expect(
      act(async () => {
        await result.current.startOAuth("github");
      }),
    ).rejects.toThrow("Network error");

    expect(window.location.href).toBe(originalHref);
  });
});

describe("AuthContext — M2: No sessionStorage for refresh tokens", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    sessionStorage.clear();
  });

  it("does not read refresh token from sessionStorage on mount", async () => {
    sessionStorage.setItem("nexus_refresh_token", "stale-token");

    renderHook(() => useAuth(), { wrapper });

    await waitFor(() => {
      expect(authApi.refreshToken).not.toHaveBeenCalled();
    });
  });

  it("does not write refresh token to sessionStorage on login", async () => {
    const accessToken = createJwt({ sub: "user1" });
    authApi.login.mockResolvedValue({
      access_token: accessToken,
      refresh_token: "new-refresh",
    });
    authApi.fetchMe.mockResolvedValue({ id: "user1", email: "a@b.com" });

    const { result } = renderHook(() => useAuth(), { wrapper });

    await act(async () => {
      await result.current.login("a@b.com", "password");
    });

    expect(sessionStorage.getItem("nexus_refresh_token")).toBeNull();
  });

  it("does not write refresh token to sessionStorage on OAuth callback", async () => {
    const accessToken = createJwt({ sub: "user1" });
    authApi.handleOAuthCallback.mockResolvedValue({
      access_token: accessToken,
      refresh_token: "oauth-refresh",
    });
    authApi.fetchMe.mockResolvedValue({ id: "user1", email: "a@b.com" });

    const params = new URLSearchParams("code=abc&state=xyz");

    const { result } = renderHook(() => useAuth(), { wrapper });

    await act(async () => {
      await result.current.handleCallback("google", params);
    });

    expect(sessionStorage.getItem("nexus_refresh_token")).toBeNull();
  });
});

describe("AuthContext — M3: Refresh mutex with retry and backoff", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("retries refresh on transient failure", async () => {
    const accessToken = createJwt({ sub: "user1" });
    authApi.login.mockResolvedValue({
      access_token: accessToken,
      refresh_token: "rt1",
    });
    authApi.fetchMe.mockResolvedValue({ id: "user1", email: "a@b.com" });

    authApi.refreshToken
      .mockRejectedValueOnce(new Error("transient"))
      .mockResolvedValueOnce({
        access_token: accessToken,
        refresh_token: "rt2",
      });

    const { result } = renderHook(() => useAuth(), { wrapper });

    await act(async () => {
      await result.current.login("a@b.com", "password");
    });

    let refreshResult;
    await act(async () => {
      refreshResult = await result.current.doRefresh();
    });

    expect(authApi.refreshToken).toHaveBeenCalledTimes(2);
    expect(refreshResult.access_token).toBe(accessToken);
  }, 15000);

  it("coalesces concurrent refresh calls into one", async () => {
    const accessToken = createJwt({ sub: "user1" });
    authApi.login.mockResolvedValue({
      access_token: accessToken,
      refresh_token: "rt1",
    });
    authApi.fetchMe.mockResolvedValue({ id: "user1", email: "a@b.com" });

    let resolveRefresh;
    const refreshPromise = new Promise((resolve) => {
      resolveRefresh = resolve;
    });
    authApi.refreshToken.mockReturnValue(refreshPromise);

    const { result } = renderHook(() => useAuth(), { wrapper });

    await act(async () => {
      await result.current.login("a@b.com", "password");
    });

    let p1, p2;
    act(() => {
      p1 = result.current.doRefresh();
      p2 = result.current.doRefresh();
    });

    expect(authApi.refreshToken).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveRefresh({
        access_token: accessToken,
        refresh_token: "rt2",
      });
    });

    const [r1, r2] = await Promise.all([p1, p2]);
    expect(r1).toEqual(r2);
    expect(r1.access_token).toBe(accessToken);
  });

  it("throws after max retries exceeded without clearing session", async () => {
    const accessToken = createJwt({ sub: "user1" });
    authApi.login.mockResolvedValue({
      access_token: accessToken,
      refresh_token: "rt1",
    });
    authApi.fetchMe.mockResolvedValue({ id: "user1", email: "a@b.com" });
    authApi.refreshToken.mockRejectedValue(new Error("permanent failure"));

    const { result } = renderHook(() => useAuth(), { wrapper });

    await act(async () => {
      await result.current.login("a@b.com", "password");
    });

    expect(result.current.isAuthenticated).toBe(true);

    let thrownError;
    await act(async () => {
      try {
        await result.current.doRefresh();
      } catch (err) {
        thrownError = err;
      }
    });

    expect(authApi.refreshToken).toHaveBeenCalledTimes(3);
    expect(thrownError).toBeDefined();
    expect(thrownError.message).toBe("permanent failure");
  }, 30000);
});
