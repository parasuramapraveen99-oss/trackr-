"use client";

import type { CSSProperties, ReactNode } from "react";
import { slipFootLabel, slipFootRow, slipFootValue, slipLabel, slipNum, slipRule, slipShell } from "./tokens";

/**
 * A ruled KPI card — the "ledger slip".
 *
 * Reads top to bottom as a torn-off slip: letterspaced metric label, the figure set
 * large in a serif, a rule, then a footer pair giving the figure its basis. The rule
 * matters — it separates "what this is" from "what it is measured against", which is
 * the distinction that stops a number being quoted without its denominator.
 */
export function VintageKpi({
  label,
  value,
  foot,
  footValue,
  negative,
  footNegative,
}: {
  label: ReactNode;
  value: ReactNode;
  foot?: ReactNode;
  footValue?: ReactNode;
  negative?: boolean;
  /** Sign of ``footValue``, which is often the opposite of the headline's. */
  footNegative?: boolean;
}) {
  return (
    <div style={slipShell}>
      <p style={slipLabel}>{label}</p>
      <p style={slipNum(negative)}>{value}</p>
      <div style={slipRule} />
      <div style={slipFootRow}>
        <span style={slipFootLabel}>{foot}</span>
        <span style={slipFootValue(footNegative)}>{footValue}</span>
      </div>
    </div>
  );
}

export interface XirrWindow {
  key: string;
  label: string;
}

/**
 * The paired Return / XIRR slip. Wider than a plain KPI (flex 1.6) because it carries
 * a window switcher alongside its label — FY, 1Y, 3Y are the same measure over
 * different spans, not different measures.
 */
export function VintageXirr({
  windows,
  activeWindow,
  onWindowChange,
  value,
  caption,
  negative = true,
}: {
  windows: XirrWindow[];
  activeWindow: string;
  onWindowChange: (key: string) => void;
  value: ReactNode;
  caption?: ReactNode;
  negative?: boolean;
}) {
  return (
    <div style={{ ...slipShell, flex: "1.6 1 160px", minWidth: 0 }}>
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          alignItems: "baseline",
          justifyContent: "flex-start",
          gap: 10,
          margin: "0 0 11px",
        }}
      >
        <p style={{ ...slipLabel, margin: 0 }}>Return · XIRR</p>
        <span style={{ display: "flex", gap: 2 }}>
          {windows.map((w) => (
            <button
              key={w.key}
              type="button"
              aria-pressed={activeWindow === w.key}
              onClick={() => onWindowChange(w.key)}
              style={{
                border: "none",
                cursor: "pointer",
                padding: "2px 7px",
                borderRadius: 2,
                fontFamily: "var(--font-label)",
                fontSize: 9,
                fontWeight: 600,
                letterSpacing: ".12em",
                textTransform: "uppercase",
                background: activeWindow === w.key ? "var(--ledger-ink)" : "transparent",
                color: activeWindow === w.key ? "var(--ledger-chip-text)" : "var(--ledger-ink-faint)",
              } as CSSProperties}
            >
              {w.label}
            </button>
          ))}
        </span>
      </div>
      <p style={slipNum(negative)}>{value}</p>
      <div style={slipRule} />
      <div style={slipFootRow}>
        <span style={slipFootLabel}>{caption}</span>
      </div>
    </div>
  );
}
