import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  fetchOAuthAuthorizeUrl,
  handleOAuthCallback,
} from "../auth";

const API = import.meta.env.VITE_API_URL || "http://localhost:8000";

describe("auth API — fetchOAuthAuthorizeUrl", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("fetches authorize URL from API", async () => {
    const fakeUrl = "https://accounts.google.com/o/oauth2/v2/auth?scope=openid";
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ authorize_url: fakeUrl }),
    });

    const url = await fetchOAuthAuthorizeUrl("google");

    expect(url).toBe(fakeUrl);
    expect(globalThis.fetch).toHaveBeenCalledWith(
      `${API}/api/v1/auth/google/authorize`,
      expect.objectContaining({
        headers: expect.objectContaining({ "Content-Type": "application/json" }),
      }),
    );
  });

  it("throws on failed fetch", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: false,
      status: 500,
      json: async () => ({ detail: "Internal error" }),
    });

    await expect(fetchOAuthAuthorizeUrl("google")).rejects.toThrow("Internal error");
  });
});

describe("auth API — handleOAuthCallback", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("calls callback endpoint with query params", async () => {
    const fakeResponse = {
      access_token: "at",
      refresh_token: "rt",
      token_type: "bearer",
    };
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => fakeResponse,
    });

    const params = new URLSearchParams("code=abc&state=xyz");
    const result = await handleOAuthCallback("google", params);

    expect(result).toEqual(fakeResponse);
    expect(globalThis.fetch).toHaveBeenCalledWith(
      `${API}/api/v1/auth/google/callback?code=abc&state=xyz`,
      expect.objectContaining({ headers: expect.objectContaining({ "Content-Type": "application/json" }) }),
    );
  });
});
