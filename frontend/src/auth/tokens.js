import {
  getAccessToken,
  getTokenExpiry,
  refreshToken as refreshTokenApi,
  clearTokens,
  clearTokenExpiry,
} from "../api/auth";

const REFRESH_AHEAD_SECONDS = 60;

let refreshPromise = null;

export function isTokenExpiringSoon() {
  const expiry = getTokenExpiry();
  if (!expiry) return true;
  return Date.now() >= expiry - REFRESH_AHEAD_SECONDS * 1000;
}

export async function getValidToken() {
  const token = getAccessToken();
  if (!token) return null;

  if (isTokenExpiringSoon()) {
    try {
      await ensureFreshToken();
    } catch {
      clearTokens();
      clearTokenExpiry();
      return null;
    }
  }

  return getAccessToken();
}

export async function ensureFreshToken() {
  if (refreshPromise) return refreshPromise;

  refreshPromise = refreshTokenApi()
    .finally(() => {
      refreshPromise = null;
    });

  return refreshPromise;
}

let refreshInterval = null;

export function startTokenRefreshLoop(onSessionExpired) {
  stopTokenRefreshLoop();

  refreshInterval = setInterval(async () => {
    const token = getAccessToken();
    if (!token) return;

    if (isTokenExpiringSoon()) {
      try {
        await ensureFreshToken();
      } catch {
        clearTokens();
        clearTokenExpiry();
        onSessionExpired?.();
      }
    }
  }, 30_000);
}

export function stopTokenRefreshLoop() {
  if (refreshInterval) {
    clearInterval(refreshInterval);
    refreshInterval = null;
  }
}
