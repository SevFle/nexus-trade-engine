const API = import.meta.env.VITE_API_URL || "http://localhost:8000";

async function request(endpoint, options = {}) {
  const res = await fetch(`${API}${endpoint}`, {
    headers: { "Content-Type": "application/json", ...options.headers },
    ...options,
  });

  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const message = body.detail || body.message || `Request failed (${res.status})`;
    const err = new Error(message);
    err.status = res.status;
    err.body = body;
    throw err;
  }

  if (res.status === 204) return null;
  return res.json();
}

export function getAccessToken() {
  return sessionStorage.getItem("nexus_access_token");
}

export function setAccessToken(token) {
  sessionStorage.setItem("nexus_access_token", token);
}

export function clearAccessToken() {
  sessionStorage.removeItem("nexus_access_token");
}

export function getRefreshToken() {
  return sessionStorage.getItem("nexus_refresh_token");
}

export function setRefreshToken(token) {
  sessionStorage.setItem("nexus_refresh_token", token);
}

export function clearRefreshToken() {
  sessionStorage.removeItem("nexus_refresh_token");
}

export function clearTokens() {
  clearAccessToken();
  clearRefreshToken();
}

export function getTokenExpiry() {
  const val = sessionStorage.getItem("nexus_token_expiry");
  return val ? Number(val) : null;
}

export function setTokenExpiry(expiresIn) {
  const now = Date.now();
  sessionStorage.setItem("nexus_token_expiry", String(now + expiresIn * 1000));
}

export function clearTokenExpiry() {
  sessionStorage.removeItem("nexus_token_expiry");
}

export function storeTokens({ access_token, refresh_token, expires_in }) {
  setAccessToken(access_token);
  setRefreshToken(refresh_token);
  if (expires_in) setTokenExpiry(expires_in);
}

export async function register({ email, password, display_name }) {
  return request("/api/v1/auth/register", {
    method: "POST",
    body: JSON.stringify({ email, password, display_name }),
  });
}

export async function login({ email, password }) {
  const data = await request("/api/v1/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
  storeTokens(data);
  return data;
}

export async function refreshToken() {
  const refresh = getRefreshToken();
  if (!refresh) return null;

  const data = await request("/api/v1/auth/refresh", {
    method: "POST",
    body: JSON.stringify({ refresh_token: refresh }),
  });
  storeTokens(data);
  return data;
}

export async function getMe() {
  const token = getAccessToken();
  if (!token) return null;

  return request("/api/v1/auth/me", {
    headers: { Authorization: `Bearer ${token}` },
  });
}

export async function logout() {
  const refresh = getRefreshToken();
  if (refresh) {
    try {
      await request("/api/v1/auth/logout", {
        method: "POST",
        body: JSON.stringify({ refresh_token: refresh }),
      });
    } catch {
      // best-effort: clear locally even if server call fails
    }
  }
  clearTokens();
  clearTokenExpiry();
}

export async function getProviders() {
  try {
    return await request("/api/v1/auth/providers");
  } catch {
    return { providers: ["local"] };
  }
}

export function getOAuthAuthorizeUrl(provider) {
  return `${API}/api/v1/auth/${provider}/authorize`;
}
