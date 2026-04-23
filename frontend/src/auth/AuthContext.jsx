import { createContext, useState, useEffect, useCallback, useRef } from "react";
import {
  login as loginApi,
  register as registerApi,
  logout as logoutApi,
  getMe,
  getProviders,
  getAccessToken,
  storeTokens,
  clearTokens,
  clearTokenExpiry,
} from "../api/auth";
import { startTokenRefreshLoop, stopTokenRefreshLoop } from "./tokens";

export const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);
  const [providers, setProviders] = useState(["local"]);
  const logoutReasonRef = useRef(null);

  const handleSessionExpired = useCallback(() => {
    logoutReasonRef.current = "session_expired";
    setUser(null);
  }, []);

  useEffect(() => {
    let cancelled = false;

    async function bootstrap() {
      try {
        const provData = await getProviders();
        if (!cancelled) {
          setProviders(provData.providers || ["local"]);
        }
      } catch {
        if (!cancelled) setProviders(["local"]);
      }

      const token = getAccessToken();
      if (!token) {
        if (!cancelled) setLoading(false);
        return;
      }

      try {
        const profile = await getMe();
        if (!cancelled) {
          setUser(profile);
          startTokenRefreshLoop(handleSessionExpired);
        }
      } catch {
        if (!cancelled) {
          clearTokens();
          clearTokenExpiry();
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    bootstrap();
    return () => {
      cancelled = true;
      stopTokenRefreshLoop();
    };
  }, [handleSessionExpired]);

  const login = useCallback(async (credentials) => {
    await loginApi(credentials);
    const profile = await getMe();
    setUser(profile);
    startTokenRefreshLoop(handleSessionExpired);
    logoutReasonRef.current = null;
    return profile;
  }, [handleSessionExpired]);

  const register = useCallback(async (fields) => {
    await registerApi(fields);
    const { email, password } = fields;
    await loginApi({ email, password });
    const profile = await getMe();
    setUser(profile);
    startTokenRefreshLoop(handleSessionExpired);
    logoutReasonRef.current = null;
    return profile;
  }, [handleSessionExpired]);

  const logout = useCallback(async () => {
    await logoutApi();
    stopTokenRefreshLoop();
    setUser(null);
    logoutReasonRef.current = null;
  }, []);

  const handleOAuthCallback = useCallback(async (tokenData) => {
    storeTokens(tokenData);
    const profile = await getMe();
    setUser(profile);
    startTokenRefreshLoop(handleSessionExpired);
    logoutReasonRef.current = null;
    return profile;
  }, [handleSessionExpired]);

  const value = {
    user,
    loading,
    providers,
    isAuthenticated: !!user,
    login,
    register,
    logout,
    handleOAuthCallback,
    getLogoutReason: () => logoutReasonRef.current,
  };

  return (
    <AuthContext.Provider value={value}>
      {children}
    </AuthContext.Provider>
  );
}
