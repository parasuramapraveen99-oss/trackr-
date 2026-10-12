import type { CSSProperties } from "react";

/**
 * Shared style constants for the ledger treatment.
 *
 * These are plain objects rather than Tailwind classes on purpose: the standalone
 * design is specified in exact pixel and letterspacing values (9px labels at .2em,
 * 32px Times figures at -.014em) that have no place on Tailwind's scale. Writing them
 * as inline style is what makes the revamped screens pixel-exact against the kit
 * rather than "close on a 4px grid".
 */

/** Ink-to-brass allocation ramp. Nine steps; weight reads as depth of colour. */
export const ALLOC_RAMP = [
  "var(--alloc-1)",
  "var(--alloc-2)",
  "var(--alloc-3)",
  "var(--alloc-4)",
  "var(--alloc-5)",
  "var(--alloc-6)",
  "var(--alloc-7)",
  "var(--alloc-8)",
  "var(--alloc-9)",
] as const;

/** Nine categorical steps, ordered so the first four read apart at donut-slice size. */
export const CHART_COLORS = [
  "var(--chart-1)",
  "var(--chart-2)",
  "var(--chart-3)",
  "var(--chart-4)",
  "var(--chart-5)",
  "var(--chart-6)",
  "var(--chart-7)",
  "var(--chart-8)",
  "var(--chart-9)",
] as const;

/** Monochrome ramp, for single-series charts and grey "Other" slices. */
export const CHART_MONO = [
  "var(--chart-mono-1)",
  "var(--chart-mono-2)",
  "var(--chart-mono-3)",
  "var(--chart-mono-4)",
  "var(--chart-mono-5)",
  "var(--chart-mono-6)",
] as const;

const ARCHIVO = 'var(--font-label)';
const TIMES = 'var(--font-slip)';
const PETRONA = 'var(--font-figure)';

/* ---- The KPI "slip": a torn-off ledger card, ruled top and mid ---- */

export const slipShell: CSSProperties = {
  position: "relative",
  padding: "18px 18px 16px",
  background: "var(--surface)",
  borderTop: "1px solid var(--ledger-rule)",
  // The ring lives in globals.css as --shadow-slip because Card wears the identical
  // treatment — one definition, so the slips and the panels below them can never drift
  // into two subtly different borders.
  boxShadow: "var(--shadow-slip)",
  flex: "1 1 160px",
  minWidth: 0,
};

export const slipLabel: CSSProperties = {
  margin: "0 0 11px",
  fontFamily: ARCHIVO,
  fontSize: 12,
  fontWeight: 700,
  textTransform: "uppercase",
  letterSpacing: ".14em",
  color: "var(--text-primary)",
};

/**
 * The panel heading in ds/Card.tsx — one step above `slipLabel` above.
 *
 * Three sizes, not one: a KPI slip names a single figure and must not compete with the
 * 32px number under it, so it stays at `slipLabel`. A card names a whole section, so it
 * gets the extra step. Tracking tightens as the size grows — .2em is right at 9px and
 * unreadable at 15px, where the letterforms have room to breathe.
 */
export const cardTitle: CSSProperties = {
  margin: 0,
  fontFamily: ARCHIVO,
  fontSize: 15,
  fontWeight: 700,
  lineHeight: 1.25,
  textTransform: "uppercase",
  letterSpacing: ".1em",
  color: "var(--text-primary)",
};

export const slipNum = (neg?: boolean): CSSProperties => ({
  margin: 0,
  fontFamily: TIMES,
  fontWeight: 400,
  fontSize: 32,
  lineHeight: 1,
  letterSpacing: "-.014em",
  color: neg ? "var(--ledger-ink-negative)" : "var(--ledger-ink)",
  fontVariantNumeric: "tabular-nums",
  whiteSpace: "nowrap",
});

export const slipRule: CSSProperties = {
  marginTop: 10,
  borderBottom: "1px solid var(--ledger-rule)",
  opacity: 0.55,
};

export const slipFootRow: CSSProperties = {
  display: "flex",
  alignItems: "baseline",
  justifyContent: "space-between",
  gap: 10,
  marginTop: 9,
};

export const slipFootLabel: CSSProperties = {
  fontFamily: ARCHIVO,
  fontSize: 9.5,
  letterSpacing: ".06em",
  textTransform: "uppercase",
  color: "var(--ledger-ink-faint)",
};

export const slipFootValue = (neg?: boolean): CSSProperties => ({
  flexShrink: 0,
  whiteSpace: "nowrap",
  fontFamily: PETRONA,
  fontSize: 13,
  fontWeight: 700,
  // The footer figure is a percentage or a basis, and on this card it is routinely
  // negative -- an unrealised loss, an XIRR. Painting it in plain --ledger-ink made a
  // loss and a gain the same colour, so the sign was carried by the minus sign alone.
  color: neg ? "var(--ledger-ink-negative)" : "var(--ledger-ink)",
  fontVariantNumeric: "tabular-nums",
});

/* ---- The ruled grid: header small-caps, body figures ---- */

export const ledgerHead: CSSProperties = {
  fontFamily: ARCHIVO,
  fontSize: 9.5,
  fontWeight: 600,
  textTransform: "uppercase",
  letterSpacing: ".14em",
  color: "var(--ledger-ink-soft)",
  whiteSpace: "nowrap",
};

export const ledgerFig: CSSProperties = {
  fontFamily: TIMES,
  fontSize: 16,
  lineHeight: 1.2,
  color: "var(--ledger-ink)",
  fontVariantNumeric: "tabular-nums",
  fontFeatureSettings: '"tnum" 1',
  whiteSpace: "nowrap",
};

/** The uppercase micro-label used for section eyebrows across the shell. */
export const eyebrow: CSSProperties = {
  font: "var(--type-label)",
  textTransform: "uppercase",
  letterSpacing: "var(--tracking-label)",
  color: "var(--text-tertiary)",
};
