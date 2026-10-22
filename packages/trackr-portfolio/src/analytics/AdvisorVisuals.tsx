"use client";

/** The advisor contract, rendered with the existing board's visual vocabulary. */
import { useId, type ReactNode } from "react";
import { decimalScale, usePrivacy } from "@trackr/core";
import type { AnalyticsResponse, MilestoneMetric, MilestoneWindow, RiskWindow } from "@trackr/core";
import { SegmentedControl } from "@trackr/ui";
import { Small, compactInr, useWidth, useTip } from "./visual";

export const PERIODS = ["1M", "3M", "6M", "1Y", "3Y", "5Y", "All"] as const;
export type AdvisorPeriod = (typeof PERIODS)[number];
export type AdvisorBasis = "recorded" | "illustration";
const KEYS: Record<AdvisorPeriod, string> = { "1M": "1m", "3M": "3m", "6M": "6m", "1Y": "1y", "3Y": "3y", "5Y": "5y", All: "available" };
const AXIS = { fill: "var(--text-tertiary)", font: "500 10px var(--font-label)" };
const INK = { fill: "var(--text-primary)", font: "500 12px var(--font-figure)" };
const readable = (reason: string) => reason.toLowerCase().replaceAll("_", " ");
export const metricTip = (metric: { status?: string | null; reasons?: string[] } | undefined) =>
  metric ? [metric.status ?? "unavailable", ...(metric.reasons ?? []).map(readable)].join(" · ") : "No supported inputs";

/** An unavailable value is a visible hatched gap, with its actual reason accessible. */
export function MissingVisual({ reason = "No supported inputs", height = 120 }: { reason?: string; height?: number }) {
  const id = useId().replaceAll(":", "");
  return <svg viewBox={`0 0 300 ${height}`} width="100%" height={height} role="img" aria-label={reason}><title>{reason}</title>
    <defs><pattern id={`missing-${id}`} width="8" height="8" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><line x1="0" x2="0" y1="0" y2="8" stroke="var(--border-strong)" strokeWidth="2" /></pattern></defs>
    {[0.25, 0.5, 0.75].map((y) => <rect key={y} x="24" y={height * y - 4} width="252" height="8" rx="4" fill={`url(#missing-${id})`} opacity="0.55" />)}
    <text x="150" y={height / 2} textAnchor="middle" dominantBaseline="middle" fill="var(--text-tertiary)" style={{ font: "24px var(--font-figure)" }}>—</text>
  </svg>;
}

export function AdvisorControls({ period, setPeriod, basis, setBasis }: { period: AdvisorPeriod; setPeriod: (value: AdvisorPeriod) => void; basis: AdvisorBasis; setBasis: (value: AdvisorBasis) => void }) {
  return <div style={{ gridColumn: "1 / -1", display: "flex", gap: 12, justifyContent: "space-between", flexWrap: "wrap", minWidth: 0 }}>
    <SegmentedControl size="sm" value={period} onChange={setPeriod} options={PERIODS.map((value) => ({ value, label: value }))} style={{ flexWrap: "wrap", maxWidth: "100%" }} />
    <SegmentedControl size="sm" value={basis} onChange={setBasis} options={[{ value: "recorded", label: "Recorded", title: "Cash-flow-adjusted returns from your transaction ledger and engine snapshots" }, { value: "illustration", label: "Hypothetical", title: "Today's constituents and weights held from the selected start; no rebalancing, costs or cash dividends" }]} />
  </div>;
}

export function selectedRisk(data: AnalyticsResponse, period: AdvisorPeriod, basis: AdvisorBasis): RiskWindow | undefined {
  return basis === "recorded" ? data.risk[KEYS[period]] : data.advisor?.illustrations[period]?.risk;
}
export function selectedWindow(data: AnalyticsResponse, period: AdvisorPeriod, basis: AdvisorBasis): MilestoneMetric<MilestoneWindow> | undefined {
  return basis === "recorded" ? data.advisor?.sections.performance.windows[period] : data.advisor?.illustrations[period]?.window;
}
export function riskBoardData(data: AnalyticsResponse, period: AdvisorPeriod, basis: AdvisorBasis): Pick<AnalyticsResponse, "risk" | "risk_contributions"> {
  const risk = selectedRisk(data, period, basis);
  const labels = new Map(data.positions.map((p) => [p.isin, p.symbol ?? p.name ?? p.isin]));
  // Recorded returns are whole-portfolio, so they cannot be split by holding. The Euler
  // shares shown instead are the health evidence's: today's weights on the last 252
  // sessions -- a different basis, which the tile label states.
  const euler = data.health.evidence?.covariance?.value?.risk_shares;
  const contributions = basis === "illustration"
    ? data.advisor?.illustrations[period]?.risk_contributions ?? []
    : data.positions.map((p) => ({ isin: p.isin, weight_pct: p.weight ?? 0, risk_share_pct: euler?.[p.isin]?.risk_share == null ? null : euler[p.isin]!.risk_share! * 100 }));
  return { risk: risk ? { "1y": risk } : {}, risk_contributions: { "1y": contributions.map((row) => ({ ...row, symbol: labels.get(row.isin) ?? row.isin })) } };
}

/** Same chart and metric endpoints, selected from one backend window. */
export function AdvisorGrowth({ data, period, basis }: { data: AnalyticsResponse; period: AdvisorPeriod; basis: AdvisorBasis }) {
  const [ref, width] = useWidth();
  const tip = useTip();
  const { masked } = usePrivacy();
  const window = selectedWindow(data, period, basis);
  const series = window?.value?.growth_series.value ?? [];
  const benchmark = window?.value?.benchmark_growth.value ?? [];
  const scale = decimalScale([...series.map((p) => p.value), ...benchmark.map((p) => p.value)]);
  const height = 250, m = { l: 12, r: 65, t: 16, b: 25 };
  if (series.length < 2) return <div ref={ref}><MissingVisual height={height} reason={metricTip(window ?? data.advisor?.illustrations[period])} /></div>;
  const t0 = Date.parse(series[0]!.date), t1 = Date.parse(series[series.length - 1]!.date);
  const X = (day: string) => m.l + (Date.parse(day) - t0) / (t1 - t0 || 1) * Math.max(1, width - m.l - m.r);
  const Y = (value: string) => height - m.b - (scale.position(value) ?? 0.5) * (height - m.b - m.t);
  const line = (rows: typeof series) => rows.map((point, i) => `${i ? "L" : "M"}${X(point.date)},${Y(point.value)}`).join("");
  return <div ref={ref}>{width > 0 && <svg width={width} height={height} role="img" aria-label={`Growth of one lakh · ${period} · ${basis}`}>
    <title>{metricTip(window)} · {series[0]!.date} – {series[series.length - 1]!.date}</title>
    {([[series, "var(--chart-1)"], [benchmark, "var(--ink-400)"]] as const).map(([rows, color], index) => rows.length > 0 && <g key={index}>
      <path d={line(rows)} fill="none" stroke={color} strokeWidth={index ? 1.5 : 2.3} />
      <text x={width - m.r + 8} y={Y(rows[rows.length - 1]!.value)} dominantBaseline="middle" style={{ ...INK, fill: color }}>{compactInr(rows[rows.length - 1]!.value, masked)}</text>
      {rows.map((point, i) => <circle key={point.date} cx={X(point.date)} cy={Y(point.value)} r={Math.max(3, 12 - rows.length / 30)} fill="transparent" onMouseMove={(event) => tip.show(event, `${point.date} · ${compactInr(point.value, masked)}`)} onMouseLeave={tip.hide} />)}
    </g>)}
    <text x={m.l} y={height - 4} style={AXIS}>{series[0]!.date}</text><text x={width - m.r} y={height - 4} textAnchor="end" style={AXIS}>{series[series.length - 1]!.date}</text>
  </svg>}</div>;
}

export function AdvisorReturnBars({ data, period, basis }: { data: AnalyticsResponse; period: AdvisorPeriod; basis: AdvisorBasis }) {
  const [ref, width] = useWidth();
  const tip = useTip();
  const win = selectedWindow(data, period, basis);
  const risk = selectedRisk(data, period, basis);
  const rows: [string, number | null | undefined, string][] = [["Return", risk?.cum_return, "cum_return"], [basis === "recorded" ? "TWRR / yr" : "CAGR", risk?.cagr, "cagr"], ["Volatility", risk?.volatility, "volatility"], ["Drawdown", risk?.max_drawdown, "max_drawdown"]];
  const bound = Math.max(0.01, ...rows.map(([, value]) => Math.abs(value ?? 0))) * 1.1;
  const X = (n: number) => 74 + (n + bound) / (2 * bound) * Math.max(1, width - 132);
  return <div ref={ref}>{width > 0 && <svg width={width} height={176} role="img" aria-label="Selected period return and risk">
    <line x1={X(0)} x2={X(0)} y1="8" y2="164" stroke="var(--border-strong)" />
    {rows.map(([label, value, key], index) => <g key={label} onMouseMove={(event) => tip.show(event, `${label} · ${metricTip(win?.value?.risk[key])}`)} onMouseLeave={tip.hide}>
      <text x="0" y={27 + index * 42} style={AXIS}>{label}</text>
      {value != null && <rect x={Math.min(X(0), X(value))} y={17 + index * 42} width={Math.max(1, Math.abs(X(value) - X(0)))} height="13" rx="3" fill={value < 0 ? "var(--negative)" : "var(--chart-1)"} />}
      <text x={width - 52} y={27 + index * 42} style={INK}>{value == null ? "—" : `${(value * 100).toFixed(1)}%`}</text>
    </g>)}
  </svg>}</div>;
}

/** Seven exact windows; absent periods stay hatched rather than borrowing All. */
export function AdvisorWindowMatrix({ data, basis }: { data: AnalyticsResponse; basis: AdvisorBasis }) {
  const [ref, width] = useWidth();
  const tip = useTip();
  const labels = ["Return", "Volatility", "Sharpe", "Sortino", "Beta", "VaR 95%"];
  const fields = ["cum_return", "volatility", "sharpe", "sortino", "beta", "var_1d_95"] as const;
  const column = Math.max(56, (width - 48) / labels.length), minWidth = 48 + column * labels.length;
  return <div ref={ref} style={{ overflowX: "auto" }}><svg width={minWidth} height={238} role="img" aria-label="All seven analytics windows">
    {labels.map((label, j) => <text key={label} x={48 + column * (j + 0.5)} y="14" textAnchor="middle" style={AXIS}>{label}</text>)}
    {PERIODS.map((period, i) => {
      const window = selectedWindow(data, period, basis), risk = selectedRisk(data, period, basis);
      return <g key={period}><text y={45 + i * 29} style={AXIS}>{period}</text>
        {fields.map((key, j) => {
          const value = risk?.[key], metric = window?.value?.risk[key];
          const text = value == null ? "—" : ["cum_return", "volatility", "var_1d_95"].includes(key) ? `${(value * 100).toFixed(1)}%` : value.toFixed(2);
          return <g key={key} onMouseMove={(event) => tip.show(event, `${period} · ${labels[j]} · ${metricTip(metric ?? window)}`)} onMouseLeave={tip.hide}>
            <rect x={48 + column * j + 2} y={28 + i * 29} width={column - 4} height="25" rx="3" fill={value == null ? "var(--surface-sunken)" : value < 0 ? "var(--negative-tint)" : "var(--positive-tint)"} />
            <text x={48 + column * (j + 0.5)} y={45 + i * 29} textAnchor="middle" style={{ ...INK, fontSize: 11 }}>{text}</text>
          </g>;
        })}
      </g>;
    })}
  </svg></div>;
}

/** Tile figure: the grade and its display score, or the honest missing-information range. */
export function AdvisorHealthFigure({ data }: { data: AnalyticsResponse }) {
  const h = data.health;
  if (h.grade != null) return <>{h.grade}<Small>{h.score}/100 · provisional</Small></>;
  const bounds = h.score_bounds;
  return <span style={{ color: "var(--text-tertiary)" }}>—<Small>
    {h.reasons?.includes("FEATURE_DISABLED") ? "disabled" : bounds ? `range ${Math.round(bounds.lower)}–${Math.round(bounds.upper)}` : "withheld"}
  </Small></span>;
}

/** One line under the bars: why the grade is withheld, or what it is not. */
export function AdvisorHealthNote({ data }: { data: AnalyticsResponse }) {
  const h = data.health;
  const style = { font: "400 12px var(--font-label)", color: "var(--text-secondary)", marginTop: 6, lineHeight: 1.5 };
  if (h.reasons?.includes("FEATURE_DISABLED")) return <div style={style}>Portfolio health is switched off on this server.</div>;
  if (h.reasons?.includes("UNRECONCILED_SEGMENTS")) return <div style={style}>Withheld: some holdings do not reconcile yet, so a whole-portfolio grade would be wrong. See Trust.</div>;
  if (h.grade == null) {
    const missing = h.components.filter((c) => c.score == null);
    return <div style={style} title={missing.map((c) => `${c.label}: ${(c.reasons ?? []).map(readable).join(" · ")}`).join("\n")}>
      Grade pending · {h.data_status?.available_components ?? h.components.length - missing.length} of {h.components.length} parts measured. Waiting on: {missing.map((c) => c.label.toLowerCase()).join(", ")}. No missing part is filled with an average.
    </div>;
  }
  return <div style={style}>House rubric, not yet calibrated: describes structure and past risk, not future returns or suitability.</div>;
}

export function AdvisorMethodCoverage({ data }: { data: AnalyticsResponse }) {
  const [ref, width] = useWidth();
  const tip = useTip();
  const report = data.advisor;
  if (!report) return null;
  const groups = [
    ["Value", Object.values(report.sections.overview).filter((x) => x && typeof x === "object" && "status" in x)],
    ["Health", data.health.components.map((c) => ({ value: c.score, status: c.status ?? "unavailable", reasons: c.reasons ?? [] }))],
    ["Factors", Object.values(report.sections.factor_score).map((x) => x.portfolio)],
    ["Risk", Object.values(report.sections.risk_return)],
    ["Periods", Object.values(report.sections.performance.windows)],
  ] as const;
  const column = width / groups.length;
  return <div ref={ref}>{width > 0 && <svg width={width} height={90} role="img" aria-label="Advisor metric availability">
    {groups.map(([label, metrics], i) => {
      const supported = metrics.filter((metric) => metric.value != null).length;
      return <g key={label} transform={`translate(${i * column},0)`} onMouseMove={(event) => tip.show(event, `${label}: ${supported}/${metrics.length} supported · ${[...new Set(metrics.flatMap((metric) => metric.reasons))].map(readable).join(" · ")}`)} onMouseLeave={tip.hide}>
        <text y="14" style={AXIS}>{label}</text>
        {metrics.map((metric, index) => <rect key={index} x={index * Math.max(1, (column - 12) / metrics.length)} y="26" width={Math.max(1, (column - 12) / metrics.length - 2)} height="24" rx="2" fill={metric.value == null ? "var(--surface-sunken)" : metric.status === "available" ? "var(--positive)" : "var(--warning)"} />)}
        <text y="74" style={INK}>{supported}/{metrics.length}</text>
      </g>;
    })}
  </svg>}</div>;
}

export function AdvisorRiskExtras({ data, period, basis }: { data: AnalyticsResponse; period: AdvisorPeriod; basis: AdvisorBasis }) {
  const risk = selectedRisk(data, period, basis), win = selectedWindow(data, period, basis);
  const items = [["VaR 99%", risk?.var_1d_99, "var_1d_99"], ["Tail loss", risk?.cvar_1d_95, "cvar_1d_95"], ["Tracking error", risk?.tracking_error, "tracking_error"], ["Information", risk?.information_ratio, "information_ratio"]] as const;
  return <div style={{ gridColumn: "1 / -1", display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(130px,1fr))", gap: 12 }}>
    {items.map(([label, value, field]) => <div key={field} className="tp-tile" title={`${metricTip(win?.value?.risk[field])} · ${risk?.observations ?? 0} daily returns / ${field.startsWith("var") || field === "cvar_1d_95" ? 252 : 30} minimum`}>
      <Small>{label}</Small><div style={{ font: "var(--type-figure-sm)", color: "var(--text-primary)" }}>{value == null ? "—" : `${(value * (field === "information_ratio" ? 1 : 100)).toFixed(2)}${field === "information_ratio" ? "" : "%"}`}</div>
      <svg viewBox="0 0 160 12" height="12" width="100%" aria-hidden="true"><rect width="160" height="6" y="3" rx="3" fill="var(--surface-sunken)" />{value != null && <rect width={160 * Math.min(1, (risk?.observations ?? 0) / (field.startsWith("var") || field === "cvar_1d_95" ? 252 : 30))} height="6" y="3" rx="3" fill="var(--chart-1)" />}</svg>
    </div>)}
  </div>;
}

export function AdvisorScope({ children }: { children: ReactNode }) { return <div style={{ gridColumn: "1 / -1", display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap", font: "500 11px var(--font-label)", color: "var(--text-tertiary)" }}>{children}</div>; }
