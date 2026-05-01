/**
 * Report an unhandled frontend exception to the backend audit trail.
 *
 * Backend: POST /api/v1/client/errors
 * Returns { error_id }. We send a caller-supplied UUID so the boundary
 * can show the user the id immediately, even if the network call is
 * still in flight.
 *
 * Failures here are deliberately swallowed — the boundary is already
 * showing a recovery UI, and a network error in error reporting must
 * not turn into another exception.
 *
 * @param {{message: string, stack?: string, componentStack?: string,
 *          url?: string, userAgent?: string, errorId: string}} report
 * @returns {Promise<{error_id: string}>}
 */
export async function reportClientError(report) {
  const apiBase = import.meta.env.VITE_API_URL || "http://localhost:8000";
  const body = JSON.stringify({
    message: report.message,
    stack: report.stack,
    component_stack: report.componentStack,
    url: report.url,
    user_agent: report.userAgent,
    error_id: report.errorId,
  });
  try {
    const res = await fetch(`${apiBase}/api/v1/client/errors`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
      // Auth not required; the endpoint is intentionally unauthenticated
      // because reporting is most likely to fire when auth is broken.
      credentials: "omit",
      keepalive: true,
    });
    if (!res.ok) return { error_id: report.errorId };
    return await res.json();
  } catch {
    return { error_id: report.errorId };
  }
}
