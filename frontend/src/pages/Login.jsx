import { useState } from "react";
import { Link, Navigate, useLocation } from "react-router-dom";
import { useAuth } from "../auth/useAuth";
import { getOAuthAuthorizeUrl } from "../api/auth";
import { Text } from "../components/primitives/Text";
import { LoadingSpinner } from "../components/feedback/LoadingSpinner";

const OAUTH_PROVIDER_CONFIG = {
  google: { label: "Sign in with Google", icon: "G" },
  github: { label: "Sign in with GitHub", icon: "GH" },
  oidc: { label: "Sign in with SSO", icon: "SSO" },
};

export default function Login() {
  const { isAuthenticated, login, providers, loading, getLogoutReason } = useAuth();
  const location = useLocation();
  const from = location.state?.from?.pathname || "/";

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-nx-black">
        <LoadingSpinner />
      </div>
    );
  }

  if (isAuthenticated) {
    return <Navigate to={from} replace />;
  }

  const logoutReason = getLogoutReason();
  const showLocal = providers.includes("local");
  const oauthProviders = providers.filter((p) => p !== "local");
  const sessionExpired = logoutReason === "session_expired";

  async function handleSubmit(e) {
    e.preventDefault();
    setError("");
    setSubmitting(true);
    try {
      await login({ email, password });
    } catch (err) {
      setError(err.message || "Login failed. Please try again.");
    } finally {
      setSubmitting(false);
    }
  }

  function handleOAuth(provider) {
    window.location.href = getOAuthAuthorizeUrl(provider);
  }

  return (
    <div className="flex min-h-screen bg-nx-black">
      <div className="flex flex-1 items-center justify-center p-lg">
        <div className="w-full max-w-sm">
          <div className="mb-3xl text-center">
            <span className="text-display-lg font-display text-nx-text-display block mb-sm">
              NEXUS
            </span>
            <Text variant="label" color="secondary">TRADE ENGINE</Text>
          </div>

          {sessionExpired && (
            <div className="mb-lg p-md rounded-lg border border-nx-warning/30 bg-nx-warning/5 text-nx-warning text-body-sm font-body" role="alert">
              Your session has expired. Please sign in again.
            </div>
          )}

          {error && (
            <div className="mb-lg p-md rounded-lg border border-nx-accent/30 bg-nx-accent/5 text-nx-accent text-body-sm font-body" role="alert">
              {error}
            </div>
          )}

          {showLocal && (
            <form onSubmit={handleSubmit} className="space-y-md">
              <div>
                <label htmlFor="email" className="block text-label font-mono uppercase text-nx-text-secondary mb-xs">
                  Email
                </label>
                <input
                  id="email"
                  type="email"
                  required
                  autoComplete="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  className="w-full px-md py-sm bg-nx-surface border border-nx-border rounded-lg text-body font-body text-nx-text-primary placeholder-nx-text-disabled focus:outline-none focus:border-nx-interactive focus:ring-1 focus:ring-nx-interactive"
                  placeholder="you@example.com"
                />
              </div>

              <div>
                <label htmlFor="password" className="block text-label font-mono uppercase text-nx-text-secondary mb-xs">
                  Password
                </label>
                <input
                  id="password"
                  type="password"
                  required
                  autoComplete="current-password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className="w-full px-md py-sm bg-nx-surface border border-nx-border rounded-lg text-body font-body text-nx-text-primary placeholder-nx-text-disabled focus:outline-none focus:border-nx-interactive focus:ring-1 focus:ring-nx-interactive"
                  placeholder="Enter your password"
                />
              </div>

              <button
                type="submit"
                disabled={submitting}
                className="w-full px-md py-sm bg-nx-interactive text-white font-body text-body font-medium rounded-lg hover:opacity-90 transition-opacity disabled:opacity-50 focus:outline-none focus:ring-2 focus:ring-nx-interactive focus:ring-offset-2 focus:ring-offset-nx-black"
              >
                {submitting ? "Signing in..." : "Sign in"}
              </button>
            </form>
          )}

          {showLocal && oauthProviders.length > 0 && (
            <div className="flex items-center gap-md my-lg">
              <div className="flex-1 border-t border-nx-border" />
              <Text variant="label" color="disabled">or sign in with</Text>
              <div className="flex-1 border-t border-nx-border" />
            </div>
          )}

          {oauthProviders.length > 0 && (
            <div className="space-y-sm">
              {oauthProviders.map((provider) => {
                const cfg = OAUTH_PROVIDER_CONFIG[provider] || { label: `Sign in with ${provider}`, icon: "?" };
                return (
                  <button
                    key={provider}
                    type="button"
                    onClick={() => handleOAuth(provider)}
                    className="w-full px-md py-sm bg-nx-surface border border-nx-border rounded-lg text-body font-body text-nx-text-primary hover:border-nx-text-secondary transition-colors focus:outline-none focus:ring-2 focus:ring-nx-interactive focus:ring-offset-2 focus:ring-offset-nx-black flex items-center justify-center gap-sm"
                  >
                    <span className="text-label font-mono font-bold">{cfg.icon}</span>
                    {cfg.label}
                  </button>
                );
              })}
            </div>
          )}

          {showLocal && (
            <p className="mt-lg text-center text-body-sm font-body text-nx-text-secondary">
              Don&apos;t have an account?{" "}
              <Link to="/register" className="text-nx-interactive hover:underline">
                Create one
              </Link>
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
