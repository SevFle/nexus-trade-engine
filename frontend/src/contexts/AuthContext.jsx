import { createContext, useContext, useState, useEffect, useCallback, useRef } from "react";
import {
  login as apiLogin,
  register as apiRegister,
  refreshToken as apiRefreshToken,
  fetchMe,
  logout as apiLogout,
  setAccessToken,
  clearAccessToken,
  handleOAuthCallback,
  fetchOAuthAuthorizeUrl,
} from "../api/auth";

const AuthContext = createContext(null);

const TOKEN_REFRESH_MARGIN_MS = 60 * 1000;
const MAX_REFRESH_RETRIES = 2;
const REFRESH_RETRY_BASE_DELAY_MS = 1000;

function parseJwtExpiry(token) {
  try {
    const payload = JSON.parse(atob(token.split(".")[1]));
    return payload.exp ? payload.exp * 1000 : null;
  } catch {
    return null;
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
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

  const doRefresh = useCallback(async () => {
    if (refreshMutexRef.current) {
      return refreshMutexRef.current;
    }

    const promise = (async () => {
      if (!refreshTokenRef.current) {
        throw new Error("No refresh token");
      }

      for (let attempt = 0; attempt <= MAX_REFRESH_RETRIES; attempt += 1) {
        try {
          const data = await apiRefreshToken(refreshTokenRef.current);
          setAccessToken(data.access_token);
          refreshTokenRef.current = data.refresh_token;
          return data;
        } catch (err) {
          const isLast = attempt === MAX_REFRESH_RETRIES;
          if (isLast) throw err;
          await sleep(REFRESH_RETRY_BASE_DELAY_MS * 2 ** attempt);
        }
      }
    })();

    refreshMutexRef.current = promise;

    try {
      const result = await promise;
      return result;
    } finally {
      refreshMutexRef.current = null;
    }
  }, []);

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
        try {
          const data = await doRefresh();
          scheduleRefresh(data.access_token);
        } catch {
          clearSession();
        }
      }, delay);
    },
    [clearSession, doRefresh],
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
    setLoading(false);

    return () => {
      if (refreshTimerRef.current) {
        clearTimeout(refreshTimerRef.current);
      }
    };
  }, []);

  const login = useCallback(
    async (email, password) => {
      const data = await apiLogin(email, password);
      setAccessToken(data.access_token);
      refreshTokenRef.current = data.refresh_token;
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
    doRefresh,
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
