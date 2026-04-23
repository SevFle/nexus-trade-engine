import { useState } from "react";
import { Link, Navigate } from "react-router-dom";
import { useAuth } from "../auth/useAuth";
import { Text } from "../components/primitives/Text";

const MIN_PASSWORD_LENGTH = 8;

export default function Register() {
  const { isAuthenticated, register, providers } = useAuth();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  if (isAuthenticated) {
    return <Navigate to="/" replace />;
  }

  if (!providers.includes("local")) {
    return <Navigate to="/login" replace />;
  }

  function validate() {
    if (!email.trim()) return "Email is required.";
    if (password.length < MIN_PASSWORD_LENGTH) return `Password must be at least ${MIN_PASSWORD_LENGTH} characters.`;
    if (password !== confirmPassword) return "Passwords do not match.";
    return null;
  }

  async function handleSubmit(e) {
    e.preventDefault();
    setError("");

    const validationError = validate();
    if (validationError) {
      setError(validationError);
      return;
    }

    setSubmitting(true);
    try {
      await register({ email: email.trim(), password, display_name: displayName.trim() || undefined });
    } catch (err) {
      setError(err.message || "Registration failed. Please try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex min-h-screen bg-nx-black">
      <div className="flex flex-1 items-center justify-center p-lg">
        <div className="w-full max-w-sm">
          <div className="mb-3xl text-center">
            <span className="text-display-lg font-display text-nx-text-display block mb-sm">
              NEXUS
            </span>
            <Text variant="label" color="secondary">CREATE ACCOUNT</Text>
          </div>

          {error && (
            <div className="mb-lg p-md rounded-lg border border-nx-accent/30 bg-nx-accent/5 text-nx-accent text-body-sm font-body" role="alert">
              {error}
            </div>
          )}

          <form onSubmit={handleSubmit} className="space-y-md">
            <div>
              <label htmlFor="display-name" className="block text-label font-mono uppercase text-nx-text-secondary mb-xs">
                Display Name
              </label>
              <input
                id="display-name"
                type="text"
                autoComplete="name"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                className="w-full px-md py-sm bg-nx-surface border border-nx-border rounded-lg text-body font-body text-nx-text-primary placeholder-nx-text-disabled focus:outline-none focus:border-nx-interactive focus:ring-1 focus:ring-nx-interactive"
                placeholder="Optional"
              />
            </div>

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
                autoComplete="new-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                minLength={MIN_PASSWORD_LENGTH}
                className="w-full px-md py-sm bg-nx-surface border border-nx-border rounded-lg text-body font-body text-nx-text-primary placeholder-nx-text-disabled focus:outline-none focus:border-nx-interactive focus:ring-1 focus:ring-nx-interactive"
                placeholder={`At least ${MIN_PASSWORD_LENGTH} characters`}
              />
            </div>

            <div>
              <label htmlFor="confirm-password" className="block text-label font-mono uppercase text-nx-text-secondary mb-xs">
                Confirm Password
              </label>
              <input
                id="confirm-password"
                type="password"
                required
                autoComplete="new-password"
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                className="w-full px-md py-sm bg-nx-surface border border-nx-border rounded-lg text-body font-body text-nx-text-primary placeholder-nx-text-disabled focus:outline-none focus:border-nx-interactive focus:ring-1 focus:ring-nx-interactive"
                placeholder="Re-enter your password"
              />
            </div>

            <button
              type="submit"
              disabled={submitting}
              className="w-full px-md py-sm bg-nx-interactive text-white font-body text-body font-medium rounded-lg hover:opacity-90 transition-opacity disabled:opacity-50 focus:outline-none focus:ring-2 focus:ring-nx-interactive focus:ring-offset-2 focus:ring-offset-nx-black"
            >
              {submitting ? "Creating account..." : "Create account"}
            </button>
          </form>

          <p className="mt-lg text-center text-body-sm font-body text-nx-text-secondary">
            Already have an account?{" "}
            <Link to="/login" className="text-nx-interactive hover:underline">
              Sign in
            </Link>
          </p>
        </div>
      </div>
    </div>
  );
}
