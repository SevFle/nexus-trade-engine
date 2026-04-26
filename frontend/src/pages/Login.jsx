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
            <Text variant="label" color="secondary">
              TRADE ENGINE
            </Text>
          </div>

          {sessionExpired && (
            <div className="mb-lg" role="alert">
              <span className="nx-bracket text-nx-warning">[SESSION EXPIRED]</span>{" "}
              <span className="text-body-sm text-nx-text-primary">
                Sign in again.
              </span>
            </div>
          )}

          {error && (
            <div className="mb-lg" role="alert">
              <span className="nx-bracket text-nx-accent">[ERROR]</span>{" "}
              <span className="text-body-sm text-nx-text-primary">{error}</span>
            </div>
          )}

          {showLocal && (
            <form onSubmit={handleSubmit} className="space-y-lg">
              <div>
                <label htmlFor="email" className="nx-label">
                  Email
                </label>
                <input
                  id="email"
                  type="email"
                  required
                  autoComplete="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  className="nx-input"
                  placeholder="you@example.com"
                />
              </div>

              <div>
                <label htmlFor="password" className="nx-label">
                  Password
                </label>
                <input
                  id="password"
                  type="password"
                  required
                  autoComplete="current-password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className="nx-input"
                  placeholder="Enter your password"
                />
              </div>

              <button
                type="submit"
                disabled={submitting}
                className="nx-btn-primary w-full"
              >
                {submitting ? "SIGNING IN…" : "SIGN IN"}
              </button>
            </form>
          )}

          {showLocal && oauthProviders.length > 0 && (
            <div className="flex items-center gap-md my-xl">
              <div className="flex-1 border-t border-nx-border" />
              <Text variant="label" color="disabled">
                OR
              </Text>
              <div className="flex-1 border-t border-nx-border" />
            </div>
          )}

          {oauthProviders.length > 0 && (
            <div className="space-y-sm">
              {oauthProviders.map((provider) => {
                const cfg =
                  OAUTH_PROVIDER_CONFIG[provider] || {
                    label: `Sign in with ${provider}`,
                    icon: "?",
                  };
                return (
                  <button
                    key={provider}
                    type="button"
                    onClick={() => handleOAuth(provider)}
                    className="nx-btn-secondary w-full"
                  >
                    <span className="font-mono mr-sm">[{cfg.icon}]</span>
                    {cfg.label.toUpperCase()}
                  </button>
                );
              })}
            </div>
          )}

          {showLocal && (
            <p className="mt-xl text-center text-body-sm font-body text-nx-text-secondary">
              Don&apos;t have an account?{" "}
              <Link
                to="/register"
                className="text-nx-interactive hover:underline"
              >
                Create one
              </Link>
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
