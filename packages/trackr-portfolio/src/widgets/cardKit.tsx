"use client";

import { useState, type ReactNode } from "react";
import { Skeleton } from "@trackr/ui";

/**
 * The few pieces the F&O, mutual-fund and capital-gains cards share, so they read as one
 * family with the dividend card: a big figure under a small label, one plain sentence in
 * readable ink (never the faint tertiary grey), a warning chip, a details toggle, and a
 * skeleton with the same shape as the loaded card.
 */

export const figureLabel = {
  display: "block",
  fontFamily: "var(--font-label)",
  fontSize: 10,
  fontWeight: 600,
  textTransform: "uppercase" as const,
  letterSpacing: ".16em",
  color: "var(--text-secondary)",
};

export function Figure({
  label,
  value,
  sub,
  className,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  className?: string;
}) {
  return (
    <div>
      <span style={figureLabel}>{label}</span>
      <div
        className={`tabular-nums ${className ?? ""}`}
        style={{ font: "var(--type-metric)", fontFamily: "var(--font-figure)", marginTop: 4 }}
      >
        {value}
      </div>
      {sub && (
        <div style={{ font: "var(--type-body-sm)", color: "var(--text-secondary)", marginTop: 2 }}>
          {sub}
        </div>
      )}
    </div>
  );
}

export function Figures({ children }: { children: ReactNode }) {
  return <div style={{ display: "flex", gap: 40, flexWrap: "wrap", alignItems: "flex-start" }}>{children}</div>;
}

/** The one sentence a card says about itself. */
export function Line({ children, tone }: { children: ReactNode; tone?: "negative" }) {
  return (
    <p
      style={{
        margin: "14px 0 0",
        font: "var(--type-body-sm)",
        color: tone === "negative" ? "var(--negative)" : "var(--text-secondary)",
      }}
    >
      {children}
    </p>
  );
}

/** Something left out, said in a few words; the full reason sits in the details. */
export function Chip({ children, title }: { children: ReactNode; title?: string }) {
  return (
    <span
      title={title}
      style={{
        display: "inline-block",
        marginTop: 14,
        padding: "4px 10px",
        font: "var(--type-body-sm)",
        color: "var(--text-primary)",
        background: "var(--surface-sunken)",
        borderLeft: "3px solid var(--warning)",
      }}
    >
      {children}
    </span>
  );
}

export function Details({ label = "Details", children }: { label?: string; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        style={{
          marginTop: 14,
          background: "none",
          border: "none",
          padding: 0,
          cursor: "pointer",
          font: "var(--type-body-sm)",
          color: "var(--text-primary)",
          textDecoration: "underline",
        }}
      >
        {open ? "Hide details" : label}
      </button>
      {open && <div style={{ marginTop: 10 }}>{children}</div>}
    </>
  );
}

/** A horizontal bar for a share of a whole (0..1), in the given colour. */
export function ShareBar({ share, color }: { share: number; color: string }) {
  return (
    <div aria-hidden style={{ height: 10, background: "var(--surface-sunken)" }}>
      <div style={{ width: `${Math.max(Math.min(share, 1), 0.01) * 100}%`, height: "100%", background: color }} />
    </div>
  );
}

/** Same shape as a loaded card: a row of figures, then a few bars. */
export function CardSkeleton({ figures = 2, bars = 3 }: { figures?: number; bars?: number }) {
  return (
    <>
      <Figures>
        {Array.from({ length: figures }, (_, i) => (
          <div key={i}>
            <Skeleton width={70} height={10} />
            <Skeleton width={i === 0 ? 140 : 100} height={26} style={{ marginTop: 6 }} />
          </div>
        ))}
      </Figures>
      <div style={{ marginTop: 20, display: "grid", gap: 10 }}>
        {Array.from({ length: bars }, (_, i) => (
          <Skeleton key={i} width={`${[82, 60, 44, 70][i % 4]}%`} height={10} />
        ))}
      </div>
    </>
  );
}
