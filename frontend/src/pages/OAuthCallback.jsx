import { useEffect, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../auth/useAuth";
import { LoadingSpinner } from "../components/feedback/LoadingSpinner";

export default function OAuthCallback() {
  const { handleOAuthCallback, isAuthenticated } = useAuth();
  const navigate = useNavigate();
  const processed = useRef(false);

  useEffect(() => {
    if (processed.current) return;

    const params = new URLSearchParams(window.location.search);
    const accessToken = params.get("access_token");
    const refreshToken = params.get("refresh_token");
    const expiresIn = params.get("expires_in");
    const error = params.get("error");

    if (error) {
      navigate("/login", { state: { oauthError: error }, replace: true });
      return;
    }

    if (!accessToken) {
      navigate("/login", { state: { oauthError: "missing_token" }, replace: true });
      return;
    }

    processed.current = true;

    const tokenData = {
      access_token: accessToken,
      refresh_token: refreshToken,
      expires_in: expiresIn ? Number(expiresIn) : undefined,
    };

    handleOAuthCallback(tokenData)
      .then(() => {
        navigate("/", { replace: true });
      })
      .catch(() => {
        navigate("/login", { state: { oauthError: "callback_failed" }, replace: true });
      });
  }, [handleOAuthCallback, navigate]);

  if (isAuthenticated) {
    navigate("/", { replace: true });
    return null;
  }

  return (
    <div className="flex items-center justify-center min-h-screen bg-nx-black">
      <LoadingSpinner />
    </div>
  );
}
