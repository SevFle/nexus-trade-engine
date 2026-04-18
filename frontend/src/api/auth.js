const API = import.meta.env.VITE_API_URL || "http://localhost:8000";

let accessToken = null;

export function getAccessToken() {
  return accessToken;
}

export function setAccessToken(token) {
  accessToken = token;
}

export function clearAccessToken() {
  accessToken = null;
}

async function request(endpoint, options = {}) {
  const url = `${API}${endpoint}`;
  const headers = { "Content-Type": "application/json", ...options.headers };

  if (accessToken && !options.noAuth) {
    headers["Authorization"] = `Bearer ${accessToken}`;
  }

  const response = await fetch(url, { ...options, headers });

  if (response.status === 401) {
    const error = new Error("Unauthorized");
    error.status = 401;
    throw error;
  }

  if (!response.ok) {
    let message = `Request failed: ${response.status}`;
    try {
      const body = await response.json();
      message = body.detail || body.message || message;
    } catch {}
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }

  if (response.status === 204) return null;
  return response.json();
}

export async function login(email, password) {
  const data = await request("/api/v1/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
  accessToken = data.access_token;
  return data;
}

export async function register(email, password, display_name) {
  const data = await request("/api/v1/auth/register", {
    method: "POST",
    body: JSON.stringify({ email, password, display_name }),
  });
  return data;
}

export async function refreshToken(refreshTokenValue) {
  const data = await request("/api/v1/auth/refresh", {
    method: "POST",
    body: JSON.stringify({ refresh_token: refreshTokenValue }),
  });
  accessToken = data.access_token;
  return data;
}

export async function fetchMe() {
  return request("/api/v1/auth/me");
}

export async function logout(refreshTokenValue) {
  try {
    await request("/api/v1/auth/logout", {
      method: "POST",
      body: JSON.stringify({ refresh_token: refreshTokenValue }),
    });
  } finally {
    accessToken = null;
  }
}

export async function fetchOAuthAuthorizeUrl(provider) {
  const data = await request(`/api/v1/auth/${provider}/authorize`, {
    noAuth: true,
  });
  return data.authorize_url;
}

export async function handleOAuthCallback(provider, searchParams) {
  const data = await request(`/api/v1/auth/${provider}/callback?${searchParams.toString()}`, {
    noAuth: true,
  });
  accessToken = data.access_token;
  return data;
}
