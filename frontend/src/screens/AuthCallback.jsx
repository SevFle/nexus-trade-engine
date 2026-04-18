import { useEffect } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useAuth } from "../contexts/AuthContext";
import { LoadingSpinner } from "../components/feedback/LoadingSpinner";

export default function AuthCallback() {
  const { handleCallback } = useAuth();
  const navigate = useNavigate();
  const { provider } = useParams();
  const [searchParams] = useSearchParams();

  useEffect(() => {
    handleCallback(provider, searchParams)
      .then(() => {
        navigate("/", { replace: true });
      })
      .catch(() => {
        navigate("/login", { replace: true, state: { error: "OAuth login failed. Please try again." } });
      });
  }, [handleCallback, provider, searchParams, navigate]);

  return (
    <div className="min-h-screen bg-gray-950 flex items-center justify-center">
      <div className="text-center">
        <LoadingSpinner />
        <p className="text-gray-400 text-sm mt-4">Completing sign in...</p>
      </div>
    </div>
  );
}
