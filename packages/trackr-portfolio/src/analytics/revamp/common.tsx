"use client";

/**
 * Shared pieces of the revamped Analytics page (analytics-revamp-review, comments 1–20).
 *
 * Every figure here arrives from the server already computed (app/analytics/revamp.py,
 * basic_slots.py); this module only formats and lays out. Nothing is recomputed in the
 * browser, and a missing value renders as "—" with its reason, never as zero.
 */

import { useState, type CSSProperties, type ReactNode } from "react";
import { Figure } from "@trackr/core";
import { cardTitle } from "@trackr/ui";
import type { AnalyticsResponse, BasicCandidate, BasicTone } from "@trackr/core";
import { EMPTY, money } from "../format";

export const TONE: Record<BasicTone, { color: string; label: string; tint: string }> = {
  needs_attention: { color: "var(--negative)", label: "Needs attention", tint: "var(--negative-tint)" },
  watch: { color: "var(--warning)", label: "Watch", tint: "color-mix(in srgb, var(--warning) 14%, transparent)" },
  positive: { color: "var(--positive)", label: "Positive", tint: "var(--positive-tint)" },
  neutral: { color: "var(--text-tertiary)", label: "Neutral", tint: "var(--surface-sunken)" },
  insufficient_data: { color: "var(--ink-300)", label: "Not measured yet", tint: "var(--surface-sunken)" },
};

export const YOU = "var(--chart-1)";
export const INDEX = "var(--ink-400)";

export const muted: CSSProperties = { font: "500 12px var(--font-label)", color: "var(--text-secondary)" };
export const faint: CSSProperties = { font: "500 11px var(--font-label)", color: "var(--text-tertiary)" };

/** Reason codes from the server, read as words. The codes stay the contract. */
export function reasonText(code: string | null | undefined): string {
  if (!code) return "";
  const known: Record<string, string> = {
    TOO_FEW_PAIRED_RETURNS: "not enough matching daily returns",
    INSUFFICIENT_PRICE_HISTORY: "listing or stored price history is shorter than the period",
    HISTORICAL_PRICES_UNAVAILABLE: "historical prices unavailable",
    NO_CLOSE_NEAR_WINDOW_END: "no official close near the start or end of the period",
    LOSS_MAKING_NO_PE: "loss-making, so no P/E",
    LOSS_MAKING_GROWTH_NOT_COMPARABLE: "loss-making: growth on a negative base is not comparable",
    NOT_APPLICABLE_FOR_LENDERS: "not applicable: borrowing is a lender's business",
    LENDER_NEEDS_GNPA_NOT_INGESTED: "lender: needs gross NPA, not yet ingested",
    FINANCIAL_NOT_CLASSIFIED: "financial company not yet classified as lender or not",
    METRIC_MISSING: "one of the two figures is missing",
    MARKET_REFERENCE_MISSING: "market reference unavailable",
    NO_RETURN_HISTORY: "no return history",
    MISSING_UNIVERSE: "Nifty 500 membership not recorded for this date",
    VENDOR_STATEMENTS_NO_EXCHANGE_FILING: "company figures are vendor-reported, not yet from exchange filings",
    INDEX_LEVELS_MISSING: "index levels missing",
    SNAPSHOT_MISSING: "no stored snapshot to check against",
    TRADES_ON_THIS_DATE: "trades on this date, so not cross-checked",
  };
  return known[code] ?? code.toLowerCase().replaceAll("_", " ");
}

/** Rupees, masked by privacy mode. */
export const Rs = ({ v, sign = false }: { v: string | null | undefined; sign?: boolean }) =>
  <Figure>{money(v ?? null, sign)}</Figure>;

export const fmtPct = (v: number | null | undefined, digits = 1, signed = false) =>
  v == null || !Number.isFinite(v) ? EMPTY : `${signed && v > 0 ? "+" : ""}${v.toFixed(digits)}%`.replace("-", "−");
/** A fraction (0.284) as a percentage. */
export const fmtFrac = (v: number | string | null | undefined, digits = 1, signed = false) =>
  v == null ? EMPTY : fmtPct(Number(v) * 100, digits, signed);
export const fmtNum = (v: number | null | undefined, digits = 2) =>
  v == null || !Number.isFinite(v) ? EMPTY : v.toFixed(digits).replace("-", "−");
/** Tiny non-zero shares as "<0.1%", never "0%" (comment 3). */
export const fmtShare = (v: number | null | undefined) =>
  v == null ? EMPTY : v > 0 && v < 0.1 ? "<0.1%" : fmtPct(v, 1);

/**
 * ⓘ — the situation first, then an expandable Calculation / Why flagged (comments 5, 12).
 * A button, so it works by keyboard and touch, not only on hover.
 */
export function Info({ title, children, calc }: { title: string; children?: ReactNode; calc?: ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <span style={{ position: "relative", display: "inline-flex" }}>
      <button type="button" aria-label={`About ${title}`} aria-expanded={open} onClick={() => setOpen((o) => !o)}
        style={{ border: 0, background: "none", padding: "0 2px", cursor: "pointer", color: "var(--text-tertiary)", font: "600 12px var(--font-label)" }}>ⓘ</button>
      {open && (
        <span role="dialog" aria-label={title} style={{
          position: "absolute", zIndex: 30, top: "1.6em", left: 0, width: "min(320px, 80vw)", padding: 12,
          background: "var(--surface-raised, var(--surface))", border: "1px solid var(--border-strong)", borderRadius: 8,
          boxShadow: "0 6px 24px rgba(0,0,0,.18)", font: "400 12px/1.5 var(--font-body)", color: "var(--text-primary)", textAlign: "left",
        }}>
          <b style={{ display: "block", marginBottom: 4 }}>{title}</b>
          {children}
          {calc && <details style={{ marginTop: 8 }}><summary style={{ cursor: "pointer", ...faint }}>Calculation / Why flagged</summary><div style={{ marginTop: 6, color: "var(--text-secondary)" }}>{calc}</div></details>}
          <button type="button" onClick={() => setOpen(false)} style={{ display: "block", marginTop: 8, border: 0, background: "none", padding: 0, cursor: "pointer", ...faint }}>Close</button>
        </span>
      )}
    </span>
  );
}

export function RuleCalc({ candidate }: { candidate: BasicCandidate }) {
  if (!candidate.rule) return null;
  return <>
    <div><b>Method:</b> {candidate.rule.method}</div>
    <div><b>Period:</b> {candidate.rule.period}</div>
    <div><b>Threshold:</b> {candidate.rule.threshold}</div>
  </>;
}

/** A card with a label row (label + ⓘ + optional right-hand control) and a body. */
/** The dashboard Card's shell (@trackr/ui Card): square corner, ledger rule on top, slip ring. */
export const CARD_SHELL: CSSProperties = {
  padding: "var(--card-padding)", background: "var(--surface)", borderTop: "1px solid var(--ledger-rule)",
  borderRadius: 0, boxShadow: "var(--shadow-slip)",
};

export function Panel({ label, info, right, span = 12, children, style }: {
  label: ReactNode; info?: ReactNode; right?: ReactNode; span?: number; children: ReactNode; style?: CSSProperties;
}) {
  return (
    <section style={{ gridColumn: `span ${span}`, minWidth: 0, display: "flex", flexDirection: "column", gap: 10, ...CARD_SHELL, ...style }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <div style={{ ...cardTitle, display: "flex", alignItems: "center", gap: 4 }}>{label}{info}</div>
        {right}
      </div>
      {children}
    </section>
  );
}

export const Grid = ({ children }: { children: ReactNode }) => (
  <div className="rv-grid" style={{ display: "grid", gridTemplateColumns: "repeat(12, minmax(0, 1fr))", gap: 12 }}>{children}</div>
);

/** One colour legend per view (comment 5). */
export function ToneLegend({ tones = ["needs_attention", "watch", "positive", "neutral"] as BasicTone[] }) {
  return <div style={{ display: "flex", gap: 12, flexWrap: "wrap", ...faint }}>
    {tones.map((t) => <span key={t} style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
      <span style={{ width: 8, height: 8, borderRadius: 4, background: TONE[t].color }} />{TONE[t].label}</span>)}
  </div>;
}

/** Each tab's one-line coverage footer (review §2 / Trust tab REVAMP). */
export function CoverageFooter({ data, extra }: { data: AnalyticsResponse; extra?: ReactNode }) {
  const c = data.revamp?.coverage;
  if (!c) return null;
  const pct = c.priced_pct;
  // Everything priced and included: nothing to name, so no line. A gap is always named.
  if (!extra && c.not_valued.length === 0 && c.priced_count != null && c.priced_count === c.total_count) return null;
  return <div style={{ ...faint, gridColumn: "1 / -1", paddingTop: 4 }}>
    Covers {c.priced_count ?? "—"} of {c.total_count ?? "—"} holdings
    {pct != null ? ` · ${pct.toFixed(0)}% of value` : " · share of value unknown (some holdings unpriced)"}
    {c.not_valued.length > 0 && ` · ${c.not_valued.length} not valued: ${c.not_valued.join(", ")}`}
    {` · As of ${c.as_of}`}
    {extra}
  </div>;
}

export interface PeriodOption { key: string; label: string; disabled?: string | null }

/** Period buttons. A window longer than the history is disabled WITH the reason (review §6). */
export function Periods({ options, value, onChange }: { options: PeriodOption[]; value: string; onChange: (key: string) => void }) {
  return <div role="radiogroup" aria-label="Period" style={{ display: "inline-flex", gap: 2, padding: 2, borderRadius: 8, background: "var(--surface-sunken)", flexWrap: "wrap" }}>
    {options.map((o) => <button key={o.key} type="button" role="radio" aria-checked={value === o.key} disabled={!!o.disabled} title={o.disabled ?? undefined}
      onClick={() => onChange(o.key)} style={{
        border: 0, borderRadius: 6, padding: "4px 10px", cursor: o.disabled ? "not-allowed" : "pointer",
        background: value === o.key ? "var(--surface)" : "transparent", opacity: o.disabled ? 0.45 : 1,
        font: "600 11.5px var(--font-label)", color: "var(--text-primary)",
      }}>{o.label}</button>)}
  </div>;
}

/** A labelled figure card: big value, a comparison line, an ⓘ. */
export function Stat({ label, value, sub, info, tone, span = 3 }: {
  label: ReactNode; value: ReactNode; sub?: ReactNode; info?: ReactNode; tone?: string; span?: number;
}) {
  return <Panel label={label} info={info} span={span}>
    <div style={{ font: "600 22px var(--font-figure)", color: tone ?? "var(--text-primary)", fontVariantNumeric: "tabular-nums" }}>{value}</div>
    {sub && <div style={muted}>{sub}</div>}
  </Panel>;
}

export const RESPONSIVE_CSS = `
.rv-kpis { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 12px; align-items: stretch; }
.rv-overview > section { min-width: 0; }
.rv-overview-insights > div:first-child { flex-direction: column; align-items: flex-start !important; gap: 10px !important; }
.rv-overview-insights > ul { flex: 1; display: flex; flex-direction: column; justify-content: space-evenly; }
.rv-overview-insights > ul > li { padding-block: 10px !important; line-height: 1.55 !important; }
@media (max-width: 1200px) { .rv-kpis { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
@media (max-width: 620px) { .rv-kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 380px) { .rv-kpis { grid-template-columns: minmax(0, 1fr); } }
@media (max-width: 900px) { .rv-grid > section, .rv-factor-cards { grid-column: span 12 !important; } }
.rv-table { width: 100%; border-collapse: collapse; font: 500 12.5px var(--font-body); font-variant-numeric: tabular-nums; }
.rv-table th { text-align: left; font: 600 13px var(--font-label); text-transform: uppercase; letter-spacing: .04em; color: var(--text-primary); padding: 6px 8px; border-bottom: 1px solid var(--border); white-space: nowrap; }
.rv-table td { padding: 7px 8px; border-bottom: 1px solid var(--border); vertical-align: top; }
.rv-table td.num, .rv-table th.num { text-align: right; }
.rv-scroll { overflow-x: auto; }
`;

const PERCENT_LEAVES = /(^weight$|^pct$|_pct$|_pp$)/;
const FRACTION_LEAVES = new Set(["cum_return", "bench_cum_return", "volatility", "bench_volatility", "max_drawdown_magnitude",
  "bench_max_drawdown_magnitude", "var_1d_95", "dividend_yield", "bench_dividend_yield", "xirr", "benchmark_xirr"]);
const MULTIPLE_LEAVES = new Set(["vol_ratio_1y", "pe", "pb", "bench_pe", "bench_pb"]);

/** A Basic fact by its id's leaf -- the same unit rule as app/narrative/registry.py. */
export function FactValue({ id, value }: { id: string; value: string | number | boolean | null | undefined }) {
  const leaf = id.split(".").pop() ?? "";
  if (value == null) return <>{EMPTY}</>;
  if (leaf.endsWith("_value")) return <Rs v={String(value)} />;
  if (MULTIPLE_LEAVES.has(leaf)) return <>{fmtNum(Number(value), 2)}×</>;
  if (FRACTION_LEAVES.has(leaf)) return <>{fmtFrac(Number(value), 1)}</>;
  if (PERCENT_LEAVES.test(leaf)) return <>{fmtShare(Number(value))}</>;
  if (typeof value === "boolean") return null;
  if (typeof value === "number") return <>{Number.isInteger(value) ? value : fmtNum(value, 2)}</>;
  return <>{String(value)}</>;
}

/** Plain labels for a finding's fact roles. Subject names and their amounts carry no label. */
const ROLE_LABEL: Record<string, string> = {
  yours: "you", index: "index", ratio: "ratio", gap: "gap", weight: "weight", share: "share",
  gross: "of", total: "net change", coverage: "covers", risk_share: "risk share", fall: "fall",
  index_fall: "index fall", peak_date: "peak", recovered_date: "recovered", beta: "beta",
  observations: "days", caution: "in Caution", count: "holdings", effective: "spread like",
  average: "average", holdings: "holdings", start: "from", end: "to", period_start: "from",
  index_date: "index as of", good: "good", mixed: "mixed", bad: "poor", na: "N/A", reference: "vs",
};

/** "IGIL 25.3% · …" -- a candidate's figures, in its own fact order. */
export function CandidateFigures({ candidate, facts }: { candidate: BasicCandidate; facts: Record<string, unknown> }) {
  // Stored JSON does not keep key order, so order explicitly: the finding's own figures,
  // then each subject name followed by its amount.
  const rank = (role: string) => { const m = /_(\d)$/.exec(role); return m ? 100 + Number(m[1]) * 2 + (role.startsWith("gain") ? 1 : 0) : 0; };
  const entries = Object.entries(candidate.facts)
    .filter(([, id]) => facts[id] != null && typeof facts[id] !== "boolean")
    .sort(([a], [b]) => rank(a) - rank(b));
  return <>{entries.map(([role, id], i) => {
    const subject = /^(name|gain)_\d$/.test(role) || role === "name";
    const label = subject ? null : role === "total" && candidate.slot === "FD.MAP_COUNT" ? "of holdings" : ROLE_LABEL[role] ?? role.replaceAll("_", " ");
    return <span key={id}>{i > 0 && (role.startsWith("gain_") ? " " : " · ")}{label && <span style={faint}>{label} </span>}
      <FactValue id={id} value={facts[id] as never} /></span>;
  })}</>;
}

/** The finding's label, following its direction where the rule name would otherwise
 * contradict it ("Move together" for holdings that move independently). */
export function findingLabel(c: BasicCandidate): string {
  if (c.slot === "DV.CO_MOVEMENT" && c.direction === "move_independently") return "Move independently";
  if (c.slot === "RR.SWINGS" && c.direction === "less_volatile") return "Calmer than the index";
  if (c.slot === "OV.VS_INDEX") return c.direction === "behind" ? "Behind the index" : "Ahead of the index";
  if (c.slot === "PF.XIRR_VS_INDEX" && c.direction) return c.direction === "behind" ? "Your money trails the index" : "Your money beats the index";
  return c.rule?.label ?? c.slot;
}
