import React, { useEffect, useRef } from "react";
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

  useEffect(() => {
    if (open) {
      previousFocus.current = document.activeElement;
      dialogRef.current?.focus();
    } else {
      previousFocus.current?.focus();
    }
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const handleEsc = (e) => {
      if (e.key === "Escape") onClose?.();
    };
    window.addEventListener("keydown", handleEsc);
    return () => window.removeEventListener("keydown", handleEsc);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center"
      role="dialog"
      aria-modal="true"
      aria-label={title}
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
          <h2 className="text-subheading font-display text-nx-text-display">
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
