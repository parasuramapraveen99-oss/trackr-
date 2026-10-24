"use client";

/**
 * Fundamentals · Advanced (review §8).
 *
 * One dataset, dated and labelled (no Reported/Validated toggle). Three cards: P/E and P/B
 * as value-weighted harmonic means (loss-makers excluded and counted), dividend yield, each
 * against the Nifty 50's published figure; the verdict word comes from the server's
 * finding, never from comparing numbers here. The Valuation Map: four lenses × Table /
 * Bubble, a Nifty 500 median reference row, status from BOTH columns, N/A with reasons.
 */

import { useMemo, useState } from "react";
import type { AnalyticsResponse, BasicCandidate, RevampLens, RevampLensRow } from "@trackr/core";
import { SegmentedControl } from "@trackr/ui";
import { useTip, useWidth } from "../visual";
import { CoverageFooter, Grid, Info, Panel, TONE, faint, fmtFrac, fmtNum, fmtPct, fmtShare, muted, reasonText } from "./common";

type LensKey = "valuation" | "growth" | "profitability" | "quality";
const LENS_LABEL: Record<LensKey, string> = { valuation: "Valuation", growth: "Growth", profitability: "Profitability", quality: "Leverage" };
const METRIC: Record<string, { label: string; fmt: (v: number | null) => string }> = {
  pe: { label: "P/E", fmt: (v) => (v == null ? "—" : `${fmtNum(v, 1)}×`) },
  pb: { label: "P/B", fmt: (v) => (v == null ? "—" : `${fmtNum(v, 1)}×`) },
  profit_growth_1y: { label: "Profit growth YoY (TTM)", fmt: (v) => fmtFrac(v, 1, true) },
  sales_growth_1y: { label: "Sales growth YoY (TTM)", fmt: (v) => fmtFrac(v, 1, true) },
  roe: { label: "ROE", fmt: (v) => fmtFrac(v, 1) },
  net_margin: { label: "Net margin", fmt: (v) => fmtFrac(v, 1) },
  debt_to_equity: { label: "Debt/Equity", fmt: (v) => (v == null ? "—" : fmtNum(v, 2)) },
  interest_cover: { label: "Interest cover", fmt: (v) => (v == null ? "—" : `${fmtNum(v, 1)}×`) },
};
const STATUS_TONE = ["positive", "watch", "needs_attention"] as const;

function verdict(c: BasicCandidate | undefined, dearer: string, cheaper: string): { label: string; tone: keyof typeof TONE } {
  if (!c) return { label: "In line with the index", tone: "neutral" };
  const word = c.magnitude_word && c.magnitude_word !== "in line" ? `${c.magnitude_word[0]!.toUpperCase()}${c.magnitude_word.slice(1)} ` : "";
  return { label: `${word}${c.direction === "cheaper" || c.direction === "lower" ? cheaper : dearer}`, tone: "neutral" };
}

function Card({ title, you, index, hint, badge, extra }: { title: string; you: string; index: string; hint: string; badge: { label: string; tone: keyof typeof TONE }; extra?: React.ReactNode }) {
  return <Panel span={4} label={title} right={<span style={{ font: "600 11px var(--font-label)", color: TONE[badge.tone].color }}>{badge.label}</span>}>
    <div style={{ display: "flex", gap: 24, alignItems: "baseline" }}>
      <div><div style={faint}>You</div><div style={{ font: "600 22px var(--font-figure)", color: "var(--chart-1)" }}>{you}</div></div>
      <div><div style={faint}>Nifty 50</div><div style={{ font: "600 22px var(--font-figure)", color: "var(--ink-400)" }}>{index}</div></div>
    </div>
    <div style={faint}>{hint}</div>
    {extra}
  </Panel>;
}

function Bubble({ lens }: { lens: RevampLens }) {
  const [ref, width] = useWidth();
  const tip = useTip();
  const clip = (k: string, v: number) => (k.endsWith("growth_1y") ? Math.max(-1, Math.min(1, v)) : v);
  const pts = lens.rows.filter((r) => r.status != null).map((r) => ({ r, x: clip(lens.x, r[lens.x] as number), y: clip(lens.y, r[lens.y] as number) }));
  const rx = lens.reference[lens.x], ry = lens.reference[lens.y];
  if (!pts.length) return <div ref={ref} style={muted}>No holding has both figures for this lens.</div>;
  const xs = [...pts.map((p) => p.x), rx ?? pts[0]!.x], ys = [...pts.map((p) => p.y), ry ?? pts[0]!.y];
  const [x0, x1] = [Math.min(...xs), Math.max(...xs)], [y0, y1] = [Math.min(...ys), Math.max(...ys)];
  const h = 300, m = { l: 44, r: 16, t: 12, b: 30 };
  const X = (v: number) => m.l + ((v - x0) / (x1 - x0 || 1)) * Math.max(1, width - m.l - m.r);
  const Y = (v: number) => h - m.b - ((v - y0) / (y1 - y0 || 1)) * (h - m.t - m.b);
  const placed: [number, number, number, number][] = [];
  return <div ref={ref}>{width > 0 && <svg width={width} height={h} role="img" aria-label={`${LENS_LABEL[lens.x === "pe" ? "valuation" : "growth"]} bubble chart`}>
    {rx != null && <line x1={X(rx)} x2={X(rx)} y1={m.t} y2={h - m.b} stroke="var(--ink-400)" strokeDasharray="3 3" />}
    {ry != null && <line x1={m.l} x2={width - m.r} y1={Y(ry)} y2={Y(ry)} stroke="var(--ink-400)" strokeDasharray="3 3" />}
    {pts.sort((a, b) => (b.r.weight ?? 0) - (a.r.weight ?? 0)).map(({ r, x, y }) => {
      const radius = 4 + Math.sqrt(r.weight ?? 0) * 3, cx = X(x), cy = Y(y);
      const tone = STATUS_TONE[lens.statuses.indexOf(r.status ?? "")] ?? "neutral";
      const box: [number, number, number, number] = [cx + radius + 2, cy - 6, cx + radius + 2 + (r.symbol ?? "").length * 6, cy + 6];
      const free = !placed.some(([a, b, c, d]) => box[0] < c && box[2] > a && box[1] < d && box[3] > b);
      if (free) placed.push(box);
      const text = `${r.symbol} · ${METRIC[lens.x]!.label} ${METRIC[lens.x]!.fmt(r[lens.x] as number)} · ${METRIC[lens.y]!.label} ${METRIC[lens.y]!.fmt(r[lens.y] as number)} · ${r.status} · weight ${fmtPct(r.weight, 1)}`;
      return <g key={r.isin} onMouseMove={(e) => tip.show(e, text)} onMouseLeave={tip.hide}>
        <circle cx={cx} cy={cy} r={radius} fill={TONE[tone].color} fillOpacity={0.35} stroke={TONE[tone].color} />
        {free && <text x={box[0]} y={cy + 4} style={{ font: "500 10px var(--font-label)", fill: "var(--text-secondary)" }}>{r.symbol}</text>}
      </g>;
    })}
    <text x={m.l} y={h - 8} style={{ font: "500 10px var(--font-label)", fill: "var(--text-tertiary)" }}>{METRIC[lens.x]!.label} {lens.x_higher_is_better ? "→ higher is better" : "→ lower is better"}</text>
    <text x={4} y={m.t + 8} style={{ font: "500 10px var(--font-label)", fill: "var(--text-tertiary)" }}>{METRIC[lens.y]!.label}</text>
  </svg>}
    <div style={faint}>Bubble size = weight. Dashed lines = Nifty 500 median.{lens.x.endsWith("growth_1y") ? " Growth axes are clipped at ±100%; hover shows the actual figure." : ""}</div>
  </div>;
}

function LensTable({ lens }: { lens: RevampLens }) {
  const [sort, setSort] = useState<{ key: string; dir: 1 | -1 }>({ key: "weight", dir: -1 });
  const rows = useMemo(() => [...lens.rows].sort((a, b) => {
    const av = a[sort.key] as number | null, bv = b[sort.key] as number | null;
    if (av == null) return 1;
    if (bv == null) return -1;
    return (av - bv) * sort.dir;
  }), [lens.rows, sort]);
  const head = (key: string, label: string) => <th className="num" style={{ cursor: "pointer" }} aria-sort={sort.key === key ? (sort.dir === 1 ? "ascending" : "descending") : "none"}
    onClick={() => setSort((s) => ({ key, dir: s.key === key ? (s.dir === 1 ? -1 : 1) : -1 }))}>{label}{sort.key === key ? (sort.dir === 1 ? " ↑" : " ↓") : ""}</th>;
  return <div className="rv-scroll"><table className="rv-table">
    <thead><tr><th>Asset</th>{head("weight", "Weight")}{head(lens.x, METRIC[lens.x]!.label)}{head(lens.y, METRIC[lens.y]!.label)}<th>Status</th></tr></thead>
    <tbody>
      <tr style={{ background: "var(--surface-sunken)" }}><td><b>{lens.reference_label}</b></td><td className="num">—</td>
        <td className="num">{METRIC[lens.x]!.fmt(lens.reference[lens.x] ?? null)}</td><td className="num">{METRIC[lens.y]!.fmt(lens.reference[lens.y] ?? null)}</td><td style={faint}>reference</td></tr>
      {rows.map((r: RevampLensRow) => {
        const i = lens.statuses.indexOf(r.status ?? "");
        return <tr key={r.isin}><td>{r.symbol}</td>
          <td className="num"><span style={{ padding: "1px 6px", borderRadius: 4, background: r.weight_flag ? "var(--negative-tint)" : undefined, color: r.weight_flag ? "var(--negative)" : undefined }}>{fmtShare(r.weight)}</span></td>
          <td className="num">{METRIC[lens.x]!.fmt(r[lens.x] as number | null)}</td><td className="num">{METRIC[lens.y]!.fmt(r[lens.y] as number | null)}</td>
          <td>{r.status ? <span style={{ color: TONE[STATUS_TONE[i] ?? "neutral"].color, fontWeight: 600 }}>{r.status}</span> : <span style={faint}>N/A · {reasonText(r.reason)}</span>}</td></tr>;
      })}
    </tbody>
  </table></div>;
}

export function FundamentalsAdvanced({ data }: { data: AnalyticsResponse }) {
  const f = data.revamp?.fundamentals;
  const [lensKey, setLensKey] = useState<LensKey>("valuation");
  const [view, setView] = useState<"table" | "bubble">("table");
  if (!f) return <Grid><Panel label="Fundamentals"><div style={muted}>Fundamentals are not available for this date.</div></Panel></Grid>;
  const cands = new Map((data.basic?.tabs.fundamentals?.candidates ?? []).map((c) => [c.slot, c]));
  const b = f.cards.benchmark;
  const lens = f.map.lenses[lensKey];
  const indexNote = `Nifty 50 published ${f.cards.benchmark_as_of ?? "(date unknown)"}`;
  return <Grid>
    <div style={{ ...faint, gridColumn: "1 / -1" }}>
      Provisional · company figures are vendor-reported, as of {f.as_of.join(" – ") || "unknown"}; their reporting basis is not yet independently verified.
      <Info title="Data source">Company ratios: cached vendor fundamentals (latest reported). Index ratios: {f.cards.benchmark_source ?? "not recorded"}.</Info>
    </div>
    <Card title="P/E · lower is cheaper" you={METRIC.pe!.fmt(f.cards.pe.value)} index={METRIC.pe!.fmt(b.pe ?? null)} badge={verdict(cands.get("FD.PE"), "dearer than the index", "cheaper than the index")}
      hint={`Value-weighted harmonic mean · ${fmtPct(f.cards.pe.coverage_pct, 0)} of value · ${indexNote}`}
      extra={f.cards.pe.loss_making.length > 0 && <div style={faint}>{f.cards.pe.loss_making.length} loss-making excluded ({fmtPct(f.cards.pe.loss_making_weight_pct, 2)} of value): {f.cards.pe.loss_making.map((p) => p.symbol).join(", ")}</div>} />
    <Card title="P/B · lower is cheaper" you={METRIC.pb!.fmt(f.cards.pb.value)} index={METRIC.pb!.fmt(b.pb ?? null)} badge={verdict(cands.get("FD.PB"), "dearer than the index", "cheaper than the index")}
      hint={`Value-weighted harmonic mean · ${fmtPct(f.cards.pb.coverage_pct, 0)} of value`} />
    <Card title="Dividend yield" you={fmtFrac(f.cards.dividend_yield.value, 2)} index={fmtFrac(b.dividend_yield ?? null, 2)} badge={verdict(cands.get("FD.YIELD"), "above the index", "below the index")}
      hint={`Value-weighted · ${fmtPct(f.cards.dividend_yield.coverage_pct, 0)} of value`} />
    <Panel label="Portfolio valuation map" right={<div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
      <SegmentedControl size="sm" value={lensKey} onChange={setLensKey} options={(Object.keys(LENS_LABEL) as LensKey[]).map((k) => ({ value: k, label: LENS_LABEL[k] }))} />
      <SegmentedControl size="sm" value={view} onChange={setView} options={[{ value: "table", label: "Table" }, { value: "bubble", label: "Bubble" }]} />
    </div>} info={<Info title="Valuation map" calc={<>Each holding&apos;s two figures against the Nifty 500 median ({f.map.universe_size ?? 0} members with data). Both better → {lens.statuses[0]}; mixed → {lens.statuses[1]}; both worse → {lens.statuses[2]}. Weight turns red at {f.map.weight_flag_pct}% of value.</>}>
      How each holding compares with the market on the selected lens.</Info>}>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        {[...lens.statuses, "N/A"].map((s, i) => <span key={s} style={{ padding: "2px 8px", borderRadius: 6, background: "var(--surface-sunken)", font: "600 11.5px var(--font-label)", color: i < 3 ? TONE[STATUS_TONE[i]!].color : "var(--text-tertiary)" }}>{s} {lens.counts[s] ?? 0}</span>)}
        <span style={faint}>of {Object.values(lens.counts).reduce((a, v) => a + v, 0)} holdings</span>
      </div>
      {view === "table" ? <LensTable lens={lens} /> : <Bubble lens={lens} />}
    </Panel>
    <CoverageFooter data={data} />
  </Grid>;
}
