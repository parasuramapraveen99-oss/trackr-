"use client";

import type { CSSProperties, ReactNode } from "react";
import { IconButton } from "./Button";

interface ModalProps {
  title: ReactNode;
  subtitle?: ReactNode;
  step?: number;
  stepCount?: number;
  onClose?: () => void;
  footer?: ReactNode;
  children?: ReactNode;
  width?: number;
  style?: CSSProperties;
}

/**
 * Centred dialog for the onboarding wizard and the import helpers. The optional step
 * counter sits above the title so the reader knows how long the flow is before they
 * read what this step asks of them.
 *
 * Renders its own scrim, so mount it inside a positioned/fixed container.
 */
export function Modal({
  title,
  subtitle,
  step,
  stepCount,
  onClose,
  footer,
  children,
  width = 560,
  style,
}: ModalProps) {
  return (
    <div
      style={{
        position: "relative",
        background: "var(--overlay-scrim)",
        padding: 32,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        ...style,
      }}
    >
      {onClose && (
        <div onClick={onClose} style={{ position: "absolute", inset: 0 }} aria-hidden="true" />
      )}
      <div
        role="dialog"
        aria-modal="true"
        style={{
          position: "relative",
          width: "100%",
          maxWidth: width,
          background: "var(--surface)",
          border: "1px solid var(--border)",
          borderRadius: "var(--radius-lg)",
          boxShadow: "var(--shadow-overlay)",
          animation: "trackr-scale-in var(--duration-base) var(--ease-smooth) both",
        }}
      >
        <header style={{ display: "flex", alignItems: "flex-start", gap: 12, padding: "20px 20px 12px" }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            {stepCount ? (
              <p
                style={{
                  margin: "0 0 6px",
                  font: "var(--type-label)",
                  textTransform: "uppercase",
                  letterSpacing: "var(--tracking-label)",
                  color: "var(--text-tertiary)",
                }}
              >
                Step {step} of {stepCount}
              </p>
            ) : null}
            <h2 style={{ font: "var(--type-h2)", color: "var(--text-primary)" }}>{title}</h2>
            {subtitle && (
              <p style={{ margin: "4px 0 0", font: "var(--type-body)", color: "var(--text-tertiary)" }}>{subtitle}</p>
            )}
          </div>
          {onClose && <IconButton name="close" label="Close" variant="plain" size={32} onClick={onClose} />}
        </header>
        <div style={{ padding: "0 20px 20px", font: "var(--type-body)", color: "var(--text-secondary)" }}>
          {children}
        </div>
        {footer && (
          <footer
            style={{
              display: "flex",
              justifyContent: "flex-end",
              gap: 8,
              borderTop: "1px solid var(--border)",
              padding: "12px 20px",
            }}
          >
            {footer}
          </footer>
        )}
      </div>
    </div>
  );
}
