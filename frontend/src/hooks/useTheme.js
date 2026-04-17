import { useState, useEffect, useCallback } from "react";

export function useTheme() {
  const [mode, setMode] = useState(() => {
    const stored = localStorage.getItem("nexus-theme");
    if (stored) return stored;
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  });

  useEffect(() => {
    const root = document.documentElement;
    root.classList.remove("dark", "light");
    root.classList.add(mode === "dark" ? "dark" : "light");
    localStorage.setItem("nexus-theme", mode);
  }, [mode]);

  const toggle = useCallback(() => setMode((m) => (m === "dark" ? "light" : "dark")), []);

  return { mode, setMode, toggle };
}
