import { createContext, useContext, useState, useEffect, useCallback, useRef } from "react";
import {
  login as apiLogin,
  register as apiRegister,
  refreshToken as apiRefreshToken,
  fetchMe,
  logout as apiLogout,
  getAccessToken,
  setAccessToken,
  clearAccessToken,
  handleOAuthCallback,
  fetchOAuthAuthorizeUrl,
} from "../api/auth";

const AuthContext = createContext(null);

const TOKEN_REFRESH_MARGIN_MS = 60 * 1000;
const MAX_REFRESH_RETRIES = 3;
const RETRY_BASE_DELAY_MS = 1000;

/*
 * ACCEPTED RISK (M2): Refresh token is persisted in sessionStorage so the
 * session survives page refreshes. sessionStorage is accessible to XSS;
 * migrating to httpOnly cookies set by the backend is recommended for
 * production. See ADR-002.
 */

function parseJwtExpiry(token) {
  try {
    const payload = JSON.parse(atob(token.split(".")[1]));
    return payload.exp ? payload.exp * 1000 : null;
  } catch {
    return null;
  }
}

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);
  const [providers, setProviders] = useState(["local"]);
  const refreshTokenRef = useRef(null);
  const refreshTimerRef = useRef(null);
  const refreshMutexRef = useRef(null);

  const clearSession = useCallback(() => {
    setUser(null);
    clearAccessToken();
    refreshTokenRef.current = null;
    refreshMutexRef.current = null;
    if (refreshTimerRef.current) {
      clearTimeout(refreshTimerRef.current);
      refreshTimerRef.current = null;
    }
  }, []);

  const refreshWithMutex = useCallback((token) => {
    if (refreshMutexRef.current) {
      return refreshMutexRef.current;
    }

    const promise = apiRefreshToken(token).finally(() => {
      refreshMutexRef.current = null;
    });

    refreshMutexRef.current = promise;
    return promise;
  }, []);

  const refreshWithRetry = useCallback(
    async (token) => {
      for (let attempt = 0; attempt < MAX_REFRESH_RETRIES; attempt++) {
        try {
          return await refreshWithMutex(token);
        } catch (err) {
          if (attempt === MAX_REFRESH_RETRIES - 1) throw err;
          await new Promise((r) =>
            setTimeout(r, RETRY_BASE_DELAY_MS * Math.pow(2, attempt)),
          );
        }
      }
    },
    [refreshWithMutex],
  );

  const scheduleRefresh = useCallback(
    (token) => {
      if (refreshTimerRef.current) {
        clearTimeout(refreshTimerRef.current);
      }

      const expiry = parseJwtExpiry(token);
      if (!expiry) return;

      const now = Date.now();
      const delay = expiry - now - TOKEN_REFRESH_MARGIN_MS;

      if (delay <= 0) return;

      refreshTimerRef.current = setTimeout(async () => {
        if (!refreshTokenRef.current) return;
        try {
          const data = await refreshWithRetry(refreshTokenRef.current);
          setAccessToken(data.access_token);
          refreshTokenRef.current = data.refresh_token;
          sessionStorage.setItem("nexus_refresh_token", data.refresh_token);
          scheduleRefresh(data.access_token);
        } catch {
          clearSession();
          sessionStorage.removeItem("nexus_refresh_token");
        }
      }, delay);
    },
    [clearSession, refreshWithRetry],
  );

  const loadUser = useCallback(async () => {
    try {
      const profile = await fetchMe();
      setUser(profile);
    } catch {
      clearSession();
    } finally {
      setLoading(false);
    }
  }, [clearSession]);

  useEffect(() => {
    const storedRefresh = sessionStorage.getItem("nexus_refresh_token");
    if (storedRefresh) {
      refreshTokenRef.current = storedRefresh;
      apiRefreshToken(storedRefresh)
        .then((data) => {
          setAccessToken(data.access_token);
          refreshTokenRef.current = data.refresh_token;
          sessionStorage.setItem("nexus_refresh_token", data.refresh_token);
          scheduleRefresh(data.access_token);
          return fetchMe();
        })
        .then((profile) => setUser(profile))
        .catch(() => {
          clearSession();
          sessionStorage.removeItem("nexus_refresh_token");
        })
        .finally(() => setLoading(false));
    } else {
      setLoading(false);
    }

    return () => {
      if (refreshTimerRef.current) {
        clearTimeout(refreshTimerRef.current);
      }
    };
  }, [clearSession, scheduleRefresh]);

  const login = useCallback(
    async (email, password) => {
      const data = await apiLogin(email, password);
      setAccessToken(data.access_token);
      refreshTokenRef.current = data.refresh_token;
      sessionStorage.setItem("nexus_refresh_token", data.refresh_token);
      scheduleRefresh(data.access_token);
      await loadUser();
      return data;
    },
    [loadUser, scheduleRefresh],
  );

  const registerAndLogin = useCallback(
    async (email, password, displayName) => {
      await apiRegister(email, password, displayName);
      await login(email, password);
    },
    [login],
  );

  const startOAuth = useCallback(async (provider) => {
    const authorizeUrl = await fetchOAuthAuthorizeUrl(provider);
    window.location.href = authorizeUrl;
  }, []);

  const handleCallback = useCallback(
    async (provider, searchParams) => {
      const data = await handleOAuthCallback(provider, searchParams);
      setAccessToken(data.access_token);
      refreshTokenRef.current = data.refresh_token;
      sessionStorage.setItem("nexus_refresh_token", data.refresh_token);
      scheduleRefresh(data.access_token);
      await loadUser();
      return data;
    },
    [loadUser, scheduleRefresh],
  );

  const logoutUser = useCallback(async () => {
    try {
      if (refreshTokenRef.current) {
        await apiLogout(refreshTokenRef.current);
      }
    } finally {
      clearSession();
      sessionStorage.removeItem("nexus_refresh_token");
    }
  }, [clearSession]);

  const value = {
    user,
    loading,
    providers,
    setProviders,
    login,
    register: registerAndLogin,
    startOAuth,
    handleCallback,
    logout: logoutUser,
    isAuthenticated: !!user,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return context;
}
