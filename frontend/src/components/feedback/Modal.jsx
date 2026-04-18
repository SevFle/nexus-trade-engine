import React, { useCallback, useEffect, useId, useRef } from "react";
import { X } from "lucide-react";
import clsx from "clsx";

export function Modal({
  open,
  onClose,
  title,
  children,
  className,
  maxWidth = "max-w-xl",
}) {
  const dialogRef = useRef(null);
  const previousFocus = useRef(null);
  const headingId = useId();

  const getFocusableElements = useCallback(() => {
    if (!dialogRef.current) return [];
    return Array.from(
      dialogRef.current.querySelectorAll(
        'a[href], button:not([disabled]), textarea, input, select, [tabindex]:not([tabindex="-1"])'
      )
    ).filter((el) => !el.hasAttribute("disabled"));
  }, []);

  useEffect(() => {
    if (open) {
      previousFocus.current = document.activeElement;
      requestAnimationFrame(() => {
        const focusable = getFocusableElements();
        if (focusable.length > 0) {
          focusable[0].focus();
        } else {
          dialogRef.current?.focus();
        }
      });
    } else {
      previousFocus.current?.focus();
    }
  }, [open, getFocusableElements]);

  useEffect(() => {
    if (!open) return;
    const handleEsc = (e) => {
      if (e.key === "Escape") onClose?.();
    };
    const handleTab = (e) => {
      if (e.key !== "Tab") return;
      const focusable = getFocusableElements();
      if (focusable.length === 0) {
        e.preventDefault();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", handleEsc);
    window.addEventListener("keydown", handleTab);
    return () => {
      window.removeEventListener("keydown", handleEsc);
      window.removeEventListener("keydown", handleTab);
    };
  }, [open, onClose, getFocusableElements]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center"
      role="dialog"
      aria-modal="true"
      aria-label={title || undefined}
      aria-labelledby={!title ? headingId : undefined}
    >
      <div
        className="absolute inset-0 bg-black/80"
        onClick={onClose}
        aria-hidden="true"
      />
      <div
        ref={dialogRef}
        tabIndex={-1}
        className={clsx(
          "relative bg-nx-surface border border-nx-border-visible rounded-2xl p-xl w-full",
          maxWidth,
          "max-h-[85vh] overflow-y-auto",
          "focus:outline-none",
          className
        )}
      >
        <div className="flex items-center justify-between mb-lg">
          <h2 id={!title ? headingId : undefined} className="text-subheading font-display text-nx-text-display">
            {title}
          </h2>
          {onClose && (
            <button
              type="button"
              onClick={onClose}
              className="text-nx-text-secondary hover:text-nx-text-display p-xs"
              aria-label="Close"
            >
              <X size={16} strokeWidth={1.5} />
            </button>
          )}
        </div>
        {children}
      </div>
    </div>
  );
}
