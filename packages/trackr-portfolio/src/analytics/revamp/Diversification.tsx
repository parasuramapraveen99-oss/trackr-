"use client";

/**
 * Diversification · Advanced (review §7, comments 13–16).
 *
 * Market-cap donut (AMFI categories, list period stated, unlisted named "Unclassified"),
 * sector donut (one taxonomy; "Others" only holds sectors under 3% and lists them),
 * the correlation matrix (diverging orange–grey–blue, N/A distinct from zero, hover with
 * pair, value, period and paired observations), concentration curve and treemap.
 */

import type { AnalyticsResponse } from "@trackr/core";
import { DonutChart } from "@trackr/ui";
import { ConcentrationCurve, HoldingsTreemap, useTip } from "../visual";
import { CoverageFooter, Grid, Info, Panel, faint, fmtNum, fmtPct, muted, reasonText } from "./common";

const UNCLASSIFIED = new Set(["UNKNOWN", "Unclassified", "Not classified", "Not on AMFI list"]);

/** −1 orange · 0 grey · +1 blue; stronger colour = stronger relationship (comment 15). */
export function corrColor(v: number): string {
  // A floor so weak but real correlations stay visible on a dark surface; 0 stays grey.
  const strength = Math.abs(v) < 0.05 ? 0 : Math.round(20 + Math.min(1, Math.abs(v)) * 70);
  return `color-mix(in srgb, ${v >= 0 ? "#3b6edc" : "#e0852b"} ${strength}%, var(--surface-sunken))`;
}

function amfiPeriod(raw: string | undefined): string {
  const m = /^(\d{1,2})([A-Za-z]{3})(\d{4})$/.exec(raw ?? "");
  return m ? `${m[1]} ${m[2]} ${m[3]}` : raw ?? "unknown period";
}

function CorrelationMatrix({ data }: { data: AnalyticsResponse }) {
  const tip = useTip();
  const c = data.revamp?.diversification.correlation;
  if (!c || c.symbols.length < 2) return <div style={muted}>Not enough holdings with a year of daily returns to compare.</div>;
  const n = c.symbols.length, cell = 30, label = 92, size = label + n * cell + 4;
  return <>
    <div className="rv-scroll"><svg viewBox={`0 0 ${size} ${size}`} width="100%" style={{ maxWidth: size }} role="img" aria-label="Correlation matrix">
      <defs><pattern id="rv-na" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="6" stroke="var(--border-strong)" strokeWidth="1.5" /></pattern></defs>
      {c.symbols.map((s, i) => <g key={s}>
        <text x={label - 6} y={label + i * cell + 19} textAnchor="end" style={{ font: "500 10px var(--font-label)", fill: "var(--text-secondary)" }}>{s}</text>
        <text transform={`translate(${label + i * cell + 18},${label - 6}) rotate(-50)`} style={{ font: "500 10px var(--font-label)", fill: "var(--text-secondary)" }}>{s}</text>
      </g>)}
      {c.cells.map((row, i) => row.map((v, j) => {
        const x = label + j * cell, y = label + i * cell;
        if (i === j) return <rect key={`${i}-${j}`} x={x} y={y} width={cell - 2} height={cell - 2} fill="var(--surface)" stroke="var(--border)" />;
        const text = v?.value == null
          ? `${c.symbols[i]} × ${c.symbols[j]}: N/A — ${reasonText(v?.reason)} (${v?.observations ?? 0} paired days)`
          : `${c.symbols[i]} × ${c.symbols[j]}: ${v.value.toFixed(2)} · ${v.start} – ${v.end} · ${v.observations} paired days`;
        return <rect key={`${i}-${j}`} x={x} y={y} width={cell - 2} height={cell - 2} rx={2}
          fill={v?.value == null ? "url(#rv-na)" : corrColor(v.value)}
          onMouseMove={(e) => tip.show(e, text)} onMouseLeave={tip.hide}><title>{text}</title></rect>;
      }))}
    </svg></div>
    <div style={{ display: "flex", alignItems: "center", gap: 8, ...faint }}>
      <span>−1</span>
      <span style={{ width: 160, height: 10, borderRadius: 5, background: "linear-gradient(90deg, #e0852b, var(--surface-sunken), #3b6edc)" }} />
      <span>+1</span>
      <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}><svg width="14" height="10"><rect width="14" height="10" fill="url(#rv-na)" stroke="var(--border)" /></svg>N/A</span>
    </div>
    <div style={muted}>Each square compares two holdings: blue means their daily returns tended to move together, orange means opposite movement, and grey means little linear relationship.</div>
    <div style={faint}>Value-weighted average {fmtNum(c.weighted_average, 2)} across these {n} holdings ({fmtPct(c.covered_weight_pct, 0)} of value, {c.holdings_total} held). A pair needs {c.min_paired_returns} matching days.</div>
  </>;
}

export function DiversificationAdvanced({ data }: { data: AnalyticsResponse }) {
  const caps = data.allocation.market_cap.map((r) => ({ label: UNCLASSIFIED.has(r.label) ? "Unclassified" : r.label, value: r.pct }));
  const capUnclassified = data.allocation.market_cap.filter((r) => UNCLASSIFIED.has(r.label)).reduce((s, r) => s + r.pct, 0);
  const sector = data.revamp?.diversification.sector;
  const sectorSlices = sector ? [
    ...sector.named.map((r) => ({ label: r.label, value: r.pct })),
    ...(sector.others_pct > 0 ? [{ label: `Others (${sector.others.length})`, value: sector.others_pct }] : []),
    ...(sector.unclassified_pct > 0 ? [{ label: "Unclassified", value: sector.unclassified_pct }] : []),
  ] : [];
  return <Grid>
    <Panel span={6} label="Company size · AMFI list" info={<Info title="Company size" calc={`AMFI's half-yearly list for the period ending ${amfiPeriod(data.revamp?.diversification.amfi_list_period)}: top 100 by full market cap are large, 101–250 mid, the rest small.`}>
      Share of your money in large, mid and small companies. A stock not on the list is shown as Unclassified, never guessed.</Info>}>
      {caps.length && capUnclassified < 99.9 ? <DonutChart slices={caps} /> : null}
      {!caps.length && <div style={muted}>No company-size data.</div>}
      {capUnclassified >= 99.9 && <div style={faint}>Company-size classification is not recorded for this date; a current classification is not applied to a past date.</div>}
    </Panel>
    <Panel span={6} label="Sectors" info={<Info title="Sectors" calc={`One taxonomy: the exchange's macro-sector. "Others" holds only sectors under ${sector?.others_rule_pct ?? 3}% each.`}>
      Where your money sits by sector.</Info>}>
      {sectorSlices.length && (sector?.unclassified_pct ?? 0) < 99.9 ? <DonutChart slices={sectorSlices} /> : null}
      {!sectorSlices.length && <div style={muted}>No sector data.</div>}
      {sector && sector.others.length > 0 && <div style={faint}>Others: {sector.others.map((o) => `${o.label} ${o.pct.toFixed(1)}%`).join(", ")}</div>}
      {sector && sector.unclassified_pct >= 99.9 && <div style={faint}>Sector classification is not recorded for this date; a current classification is not applied to a past date.</div>}
    </Panel>
    <Panel label="How your holdings move together" info={<Info title="Correlation" calc="Pearson correlation of adjusted daily returns over matched sessions in the past year. Corporate actions are adjusted; a pair with too few shared days is N/A, never zero.">
      Calculated from matched daily returns over the stated period. Correlation describes historical co-movement and can change, especially during market stress. Each pair shows its data coverage.</Info>}>
      <div style={muted}>Daily-return correlations over the past year · {data.revamp?.diversification.correlation.symbols.length ?? 0} largest holdings</div>
      <CorrelationMatrix data={data} />
    </Panel>
    <Panel span={5} label="Effective holdings" info={<Info title="Effective holdings" calc="1 ÷ Σ(weight²).">How many equal-sized holdings would give the same concentration. It measures position size, not independent risks.</Info>}>
      <div style={{ font: "600 22px var(--font-figure)" }}>{fmtNum(data.derived.effective_holdings, 1)} <span style={muted}>of {data.positions.length} holdings</span></div>
      <ConcentrationCurve data={data} />
    </Panel>
    <Panel span={7} label="Holdings by weight" info={<Info title="Holdings by weight">Each box is one holding, sized by its share of your money.</Info>}>
      <HoldingsTreemap data={data} />
    </Panel>
    <CoverageFooter data={data} />
  </Grid>;
}
