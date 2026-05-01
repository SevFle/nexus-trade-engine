import React from "react";
import { reportClientError } from "../api/clientErrors";

/**
 * React error boundary. Catches render-time exceptions in its subtree,
 * reports them to the backend audit trail, and renders a recovery UI
 * with a copyable error id.
 *
 * Hooks cannot catch render exceptions; this stays a class component.
 *
 * Props:
 *   - children: subtree to guard
 *   - scope: short label included in the recovery message
 *           (e.g. "page", "widget"). Lets per-route boundaries say
 *           "this page failed" while the top-level says "the app
 *           failed."
 *   - fallback: optional render prop
 *           ({ error, errorId, reset }) => ReactNode
 *           overriding the default recovery UI
 */
export class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null, errorId: null };
  }

  static getDerivedStateFromError(error) {
    // Generate a stable id at render time — synchronous so the UI can
    // print it before the network call completes. Modern browsers
    // ship crypto.randomUUID; fall back to a manual UUID-v4-shaped
    // hex if the polyfill is missing.
    const errorId =
      typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
        ? crypto.randomUUID()
        : _uuidV4Shape();
    return { error, errorId };
  }

  componentDidCatch(error, info) {
    const { errorId } = this.state;
    if (!errorId) return;
    reportClientError({
      message: error?.message || String(error),
      stack: error?.stack,
      componentStack: info?.componentStack,
      url: typeof window !== "undefined" ? window.location.href : undefined,
      userAgent:
        typeof navigator !== "undefined" ? navigator.userAgent : undefined,
      errorId,
    });
  }

  reset = () => {
    this.setState({ error: null, errorId: null });
  };

  render() {
    const { error, errorId } = this.state;
    if (!error) return this.props.children;

    if (typeof this.props.fallback === "function") {
      return this.props.fallback({ error, errorId, reset: this.reset });
    }

    const scope = this.props.scope || "view";
    return (
      <div
        role="alert"
        className="flex flex-col items-center justify-center gap-md p-xl text-nx-text-primary"
        style={{ minHeight: 240 }}
      >
        <span className="text-label font-mono uppercase text-nx-accent">
          ERROR — {scope}
        </span>
        <h1 className="text-heading font-display text-nx-text-display">
          Something broke.
        </h1>
        <p className="text-body text-nx-text-secondary text-center max-w-md">
          The {scope} hit an unexpected exception. The error has been
          reported. Try again, or copy the id below into a support ticket
          so we can correlate it with the audit log.
        </p>
        <code className="font-mono text-label bg-nx-surface border border-nx-border rounded-md px-md py-sm select-all">
          {errorId}
        </code>
        <div className="flex gap-sm">
          <button
            type="button"
            onClick={this.reset}
            className="bg-nx-accent-subtle text-nx-text-display border border-nx-border rounded-md px-md py-sm font-mono uppercase text-label hover:bg-nx-accent hover:text-white transition-colors"
          >
            Try again
          </button>
          <button
            type="button"
            onClick={() => {
              if (typeof window !== "undefined") window.location.href = "/";
            }}
            className="bg-nx-surface text-nx-text-primary border border-nx-border rounded-md px-md py-sm font-mono uppercase text-label hover:bg-nx-accent-subtle transition-colors"
          >
            Go home
          </button>
        </div>
        {import.meta.env?.DEV && error?.stack ? (
          <details className="mt-md w-full max-w-3xl">
            <summary className="cursor-pointer text-label font-mono uppercase text-nx-text-secondary">
              Stack (dev mode)
            </summary>
            <pre className="text-label font-mono whitespace-pre-wrap bg-nx-surface border border-nx-border rounded-md p-md mt-sm overflow-auto">
              {error.stack}
            </pre>
          </details>
        ) : null}
      </div>
    );
  }
}

function _uuidV4Shape() {
  // Crypto-randomness fallback. Not used in production browsers
  // (crypto.randomUUID exists everywhere modern); included so the
  // boundary still works in test/jsdom environments without crypto.
  const r = () => Math.floor(Math.random() * 16).toString(16);
  let out = "";
  for (let i = 0; i < 32; i += 1) {
    if (i === 8 || i === 12 || i === 16 || i === 20) out += "-";
    if (i === 12) out += "4";
    else if (i === 16) out += ((Math.random() * 4) | 8).toString(16);
    else out += r();
  }
  return out;
}

export default ErrorBoundary;
