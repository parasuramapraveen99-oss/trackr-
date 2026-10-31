"use client";

import Link from "next/link";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  Dec, decimalScale, formatDate, formatMoney, formatPercent, parseDecimal, useAuth, useFetch,
  usePortfolioRefresh, usePrivacy,
} from "@trackr/core";
import type {
  FundAnalyticsOut, FundCheckOut, FundCheckStatus, FundContributionOut, FundGroupOut, FundRowOut,
  FundStatsOut, FundWindowOut,
} from "@trackr/core";
import {
  Banner, DonutChart, Input, LedgerGrid, LedgerSigned, LedgerTag, SegmentedControl, Skeleton, TabBar,
  VintageLinePlot, type LedgerColumn,
} from "@trackr/ui";
import { ErrorState } from "../widgets/PageState";
import { compactInr, useTip, useWidth } from "./visual";

/**
 * Mutual fund analytics: their own tabs, kept apart from the equity board. Shown in the
 * Analytics page's "Mutual Funds" view, above FundsCard.
 *
 * LAYOUT. Five tabs, each with a Basic view (one card that says the headline in words) and
 * an Advanced view (the cards and tables behind it) -- the progressive disclosure the
 * mutual-fund research spec recommends (product/UX_SPEC.md: detail modes change density,
 * never the calculation). Drawn in Trackr+'s own tokens, so it follows the light and dark
 * themes; nothing here is a score, a grade or a "safe zone".
 *
 * DATA. Everything is read from `/funds/analytics` (one replay of the fund ledger), so the
 * tabs cannot disagree. Two kinds of return appear and are labelled apart: YOUR return
 * (XIRR, gain by fund) and the CURRENT MIX's -- today's funds at today's weights through
 * past months -- which is what the risk figures and the index comparison describe.
 *
 * Not shown, because Trackr+ has no source for them yet: what the funds hold inside
 * (sectors, stocks, overlap), expense ratios, category peers and exit loads.
 */

type View = "overview" | "diversification" | "risk" | "performance" | "diagnostics";
type Detail = "basic" | "advanced";
const VIEWS: { key: View; label: string }[] = [
  { key: "overview", label: "Overview" },
  { key: "diversification", label: "Diversification" },
  { key: "risk", label: "Risk & return" },
  { key: "performance", label: "Performance" },
  { key: "diagnostics", label: "Diagnostics" },
];
const WINDOWS = ["1Y", "3Y", "5Y"] as const;
type WindowKey = (typeof WINDOWS)[number];
const DETAIL_KEY = "trackr.fund_detail";

const MIX = "var(--chart-1)", IDX = "var(--ink-400)";
const PALETTE = ["var(--chart-1)", "var(--chart-2)", "var(--chart-3)", "var(--chart-4)", "var(--chart-5)", "var(--ink-400)"];
const AXIS: React.CSSProperties = { fill: "var(--chart-axis-text)", font: "500 10.5px var(--font-label)" };
const INK2: React.CSSProperties = { fill: "var(--text-secondary)", font: "500 11px var(--font-label)" };

const FA_CSS = `
.fa-grid{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:16px;align-items:stretch}
.fa-panel{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-lg);padding:20px 22px;min-width:0;display:flex;flex-direction:column;gap:14px}
.fa-title{font:600 17px/1.25 var(--font-body);color:var(--text-primary);margin:0}
.fa-sub{font:500 12.5px/1.45 var(--font-label);color:var(--text-tertiary);margin:3px 0 0}
.fa-pill{display:inline-flex;align-items:center;gap:6px;border-radius:999px;padding:2px 9px;font:600 10.5px var(--font-label);letter-spacing:.08em;text-transform:uppercase;white-space:nowrap}
.fa-link{background:none;border:none;padding:0;cursor:pointer;color:var(--chart-1);font:600 12.5px var(--font-label)}
.fa-chip{border:1px solid var(--border);background:var(--surface);color:var(--text-secondary);border-radius:999px;padding:4px 11px;font:500 12px var(--font-label);cursor:pointer}
.fa-chip[aria-pressed="true"]{background:var(--text-primary);color:var(--surface);border-color:var(--text-primary)}
@container (max-width:980px){.fa-grid>*{grid-column:1 / -1!important}}
@media (max-width:980px){.fa-grid>*{grid-column:1 / -1!important}}
`;

const num = (s: string | null | undefined) => parseDecimal(s)?.toNumber() ?? null;
const isNeg = (s: string | null | undefined) => {
  const d = parseDecimal(s);
  return d !== null && d.isNegative() && !d.isZero();
};
const toneOf = (s: string | null | undefined) =>
  num(s) === null ? "var(--text-tertiary)" : isNeg(s) ? "var(--negative)" : "var(--positive)";
const ratio = (s: string | null | undefined) => {
  const d = parseDecimal(s);
  return d === null ? "—" : d.toFixed(2);
};
/** Every fund's rupee gain, summed in Decimal. */
const totalGain = (rows: FundContributionOut[]) =>
  rows.reduce((acc, c) => acc.plus(parseDecimal(c.gain) ?? 0), new Dec(0));
const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
/** A signed rate, or a plain dash with its reason on hover -- never an arrow beside "—". */
const Signed = ({ value, why }: { value: string | null; why?: string | null }) =>
  value === null ? <span title={why ?? undefined} style={{ color: "var(--text-tertiary)" }}>—</span> : <LedgerSigned value={formatPercent(value)} />;
/** A wide ledger scrolls inside its card on a phone instead of squeezing its columns. */
const Wide = ({ children }: { children: ReactNode }) => (
  <div style={{ overflowX: "auto", minWidth: 0 }}>{children}</div>
);
const Note = ({ children }: { children: ReactNode }) => (
  <p style={{ margin: 0, font: "500 12px/1.5 var(--font-label)", color: "var(--text-tertiary)" }}>{children}</p>
);
const Pill = ({ color, children }: { color: string; children: ReactNode }) => (
  <span className="fa-pill" style={{ color, background: `color-mix(in srgb, ${color} 14%, transparent)` }}>{children}</span>
);

/** The card every view is built from: a sentence-case title, a subtitle, an action. */
function Panel({ span = 12, title, sub, action, tint, children }: {
  span?: number; title?: ReactNode; sub?: ReactNode; action?: ReactNode; tint?: string; children?: ReactNode;
}) {
  return (
    <section className="fa-panel" style={{
      gridColumn: `span ${span}`,
      ...(tint ? { background: `color-mix(in srgb, ${tint} 9%, var(--surface))`, borderColor: `color-mix(in srgb, ${tint} 35%, var(--border))` } : {}),
    }}>
      {(title || action) && (
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12, flexWrap: "wrap" }}>
          <div style={{ minWidth: 0 }}>
            {title && <h3 className="fa-title">{title}</h3>}
            {sub && <p className="fa-sub">{sub}</p>}
          </div>
          {action}
        </div>
      )}
      {children}
    </section>
  );
}

/** A headline figure card: label and tag on top, the figure, one line under it. */
function Kpi({ label, tag, figure, color, foot }: { label: string; tag?: ReactNode; figure: ReactNode; color?: string; foot?: ReactNode }) {
  return (
    <Panel span={4}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
        <span style={{ font: "600 11px var(--font-label)", letterSpacing: ".12em", textTransform: "uppercase", color: "var(--text-tertiary)" }}>{label}</span>
        {tag}
      </div>
      <div style={{ font: "500 30px/1.05 var(--font-figure)", fontVariantNumeric: "tabular-nums", color: color ?? "var(--text-primary)" }}>{figure}</div>
      {foot && <Note>{foot}</Note>}
    </Panel>
  );
}

const SwitchFoot = ({ question, onAdvanced }: { question: string; onAdvanced: () => void }) => (
  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, borderTop: "1px solid var(--border)", paddingTop: 12, font: "500 12.5px var(--font-label)", color: "var(--text-tertiary)" }}>
    <span>{question}</span>
    <button type="button" className="fa-link" onClick={onAdvanced}>Switch to Advanced →</button>
  </div>
);

/** A /funds/analytics fetch already made by the page (so its header shares the response). */
interface FundAnalyticsResult { data: FundAnalyticsOut | null; error: string | null; refetch: () => void }

export function FundAnalytics({ result }: { result?: FundAnalyticsResult } = {}) {
  const { investorId, token, asOf, advisedOnly } = useAuth();
  const [view, setView] = useState<View>("overview");
  const [win, setWin] = useState<WindowKey>("3Y");
  const [detail, setDetailState] = useState<Detail>("basic");
  // Remembered per browser, like the Show choice; read after mount so the first render
  // matches whatever the server drew.
  useEffect(() => {
    try {
      if (localStorage.getItem(DETAIL_KEY) === "advanced") setDetailState("advanced");
    } catch {
      // Storage blocked: Basic is the default, just not remembered.
    }
  }, []);
  const setDetail = (d: Detail) => {
    setDetailState(d);
    try {
      localStorage.setItem(DETAIL_KEY, d);
    } catch {
      // Not remembered across visits; the choice still applies now.
    }
  };
  const own = useFetch<FundAnalyticsOut>(
    investorId && !result ? `/investors/${investorId}/funds/analytics` : null,
    { as_of: asOf, advised_only: advisedOnly || undefined },
    token,
  );
  const res = result ?? own;
  usePortfolioRefresh(own.refetch);
  const data = res.data;
  const window = data?.windows.find((w) => `${w.years}Y` === win) ?? null;
  const advanced = () => setDetail("advanced");

  if (res.error && !data) return <ErrorState message={res.error} />;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <style>{FA_CSS}</style>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <TabBar tabs={VIEWS.map((v) => ({ key: v.key, label: v.label }))} active={view} onChange={(k) => setView(k as View)} />
        <SegmentedControl size="sm" value={detail} onChange={setDetail}
          options={[{ value: "basic", label: "Basic" }, { value: "advanced", label: "Advanced" }]} />
      </div>
      {!data ? (
        <Skeleton height={220} />
      ) : (
        <>
          {data.unvalued.length > 0 && (
            <Banner tone="warning" title={`${plural(data.unvalued.length, "fund")} left out of these figures`}>
              {data.unvalued.map((u) => `${u.name}: ${u.reason}`).join(" · ")}
            </Banner>
          )}
          <div className="fa-grid">
            {view === "overview" && (detail === "basic"
              ? <OverviewBasic data={data} onAdvanced={advanced} />
              : <OverviewAdvanced data={data} goDiagnostics={() => setView("diagnostics")} />)}
            {view === "diversification" && (detail === "basic"
              ? <DiversificationBasic data={data} onAdvanced={advanced} />
              : <DiversificationAdvanced data={data} />)}
            {view === "risk" && (detail === "basic"
              ? <RiskBasic window={data.windows.find((w) => w.years === 3) ?? null} onAdvanced={advanced} />
              : <RiskAdvanced window={window} win={win} setWin={setWin} />)}
            {view === "performance" && (detail === "basic"
              ? <PerformanceBasic data={data} window={data.windows.find((w) => w.years === 3) ?? null} onAdvanced={advanced} />
              : <PerformanceAdvanced data={data} window={window} win={win} setWin={setWin} />)}
            {view === "diagnostics" && (detail === "basic"
              ? <DiagnosticsBasic data={data} onAdvanced={advanced} />
              : <DiagnosticsAdvanced data={data} />)}
          </div>
        </>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ overview ----- */

function OverviewBasic({ data, onAdvanced }: { data: FundAnalyticsOut; onAdvanced: () => void }) {
  const warn = data.checks.filter((c) => c.status === "warn");
  const direct = data.plan_split.find((g) => g.label === "Direct");
  return (
    <>
      <OverviewKpis data={data} />
      <Panel title="Your mutual funds at a glance" sub={`${plural(data.funds.length, "fund")}, valued at each scheme's AMFI NAV`}>
        <Lines lines={[
          <>Worth <b>{formatMoney(data.market_value)}</b> against <b>{formatMoney(data.invested)}</b> invested.</>,
          <>Your return on the money you put in is <b style={{ color: toneOf(data.xirr) }}>{formatPercent(data.xirr)}</b> a year (XIRR).</>,
          <><b>{formatPercent(direct?.share ?? "0", 1)}</b> of your fund value is in Direct plans, which pay no distributor commission.</>,
          warn.length ? <>{plural(warn.length, "thing")} worth a look: {warn.map((c) => c.title.toLowerCase()).join(", ")}.</> : <>No checks need attention.</>,
        ]} />
        <SwitchFoot question="Want the plan split and fund-by-fund table?" onAdvanced={onAdvanced} />
      </Panel>
    </>
  );
}

function OverviewKpis({ data }: { data: FundAnalyticsOut }) {
  const direct = data.plan_split.find((g) => g.label === "Direct");
  const invested = parseDecimal(data.invested);
  const gainPct = invested && !invested.isZero() ? parseDecimal(data.unrealised)?.dividedBy(invested).toString() ?? null : null;
  return (
    <>
      <Kpi label="Portfolio value" tag={<Pill color="var(--text-secondary)">{plural(data.funds.length, "fund")}</Pill>}
        figure={formatMoney(data.market_value)} foot={`At AMFI NAV · ${formatDate(data.as_of)}`} />
      <Kpi label="Total invested" tag={<Pill color="var(--positive)">{formatPercent(direct?.share ?? "0", 0)} Direct</Pill>}
        figure={formatMoney(data.invested)} foot="Cost of the units you hold" />
      <Kpi label="Overall gain" tag={<Pill color={toneOf(gainPct)}>{isNeg(gainPct) ? "▼" : "▲"} {formatPercent(gainPct, 1)}</Pill>}
        figure={formatMoney(data.unrealised)} color={toneOf(data.unrealised)}
        foot={<>XIRR <b style={{ color: toneOf(data.xirr) }}>{formatPercent(data.xirr)}</b> a year · realised so far {formatMoney(data.realised_to_date)}</>} />
    </>
  );
}

function OverviewAdvanced({ data, goDiagnostics }: { data: FundAnalyticsOut; goDiagnostics: () => void }) {
  const warn = data.checks.filter((c) => c.status === "warn");
  const direct = data.plan_split.find((g) => g.label === "Direct");
  const regular = data.plan_split.find((g) => g.label === "Regular");
  return (
    <>
      <OverviewKpis data={data} />
      <Panel span={7} title="Plan split" sub="Direct and Regular plans of the same scheme">
        <div style={{ display: "flex", justifyContent: "space-between", font: "600 12.5px var(--font-label)" }}>
          <span style={{ color: "var(--positive)" }}>Direct (no commission)</span>
          <span style={{ color: "var(--warning)" }}>Regular (pays commission)</span>
        </div>
        <div style={{ display: "flex", height: 12, borderRadius: 999, overflow: "hidden", background: "var(--ink-100)" }}>
          <div style={{ width: `${(num(direct?.share) ?? 0) * 100}%`, background: "var(--positive)" }} />
          <div style={{ width: `${(num(regular?.share) ?? 0) * 100}%`, background: "var(--warning)" }} />
        </div>
        <div style={{ display: "flex", justifyContent: "space-between", gap: 16, flexWrap: "wrap" }}>
          {[["Direct", direct, "var(--positive)"], ["Regular", regular, "var(--warning)"]].map(([label, g, color]) => (
            <div key={label as string} style={{ display: "grid", gap: 6, justifyItems: label === "Regular" ? "end" : "start" }}>
              <span style={{ display: "flex", gap: 8, alignItems: "center", font: "600 13px var(--font-label)" }}>
                {label as string} <Pill color={color as string}>{formatPercent((g as FundGroupOut | undefined)?.share ?? "0", 0)} · {plural((g as FundGroupOut | undefined)?.count ?? 0, "fund")}</Pill>
              </span>
              <span style={{ font: "500 24px var(--font-figure)" }}>{formatMoney((g as FundGroupOut | undefined)?.value ?? "0")}</span>
            </div>
          ))}
        </div>
        <Note>A Regular plan pays the distributor a commission out of the fund&apos;s expenses, so it returns a little less than the Direct plan of the same scheme.</Note>
      </Panel>
      <Panel span={5} title={<>Needs attention <span className="fa-sub" style={{ display: "inline" }}>({plural(warn.length, "check")} flagged)</span></>}
        action={<button type="button" className="fa-link" onClick={goDiagnostics}>All diagnostics →</button>}>
        {warn.length === 0 ? <Note>Nothing flagged right now.</Note> : warn.map((c) => (
          <div key={c.key} style={{ borderRadius: "var(--radius-md)", padding: "10px 12px", background: "color-mix(in srgb, var(--warning) 10%, var(--surface))", border: "1px solid color-mix(in srgb, var(--warning) 30%, var(--border))" }}>
            <div style={{ font: "600 13px var(--font-label)", color: "var(--text-primary)" }}>{c.title}</div>
            <div style={{ font: "500 12.5px/1.45 var(--font-label)", color: "var(--text-secondary)", marginTop: 2 }}>{c.summary}</div>
          </div>
        ))}
      </Panel>
      <MyFunds data={data} />
    </>
  );
}

function MyFunds({ data }: { data: FundAnalyticsOut }) {
  const [q, setQ] = useState("");
  const [plan, setPlan] = useState<"All" | "Direct" | "Regular">("All");
  const [asset, setAsset] = useState("All");
  const [page, setPage] = useState(0);
  const PER_PAGE = 10;
  const assets = ["All", ...data.asset_classes.map((g) => g.label)];
  const rows = data.funds.filter((f) =>
    (plan === "All" || f.plan === plan)
    && (asset === "All" || f.asset_class === asset)
    && `${f.name} ${f.amc ?? ""} ${f.category ?? ""} ${f.isin}`.toLowerCase().includes(q.trim().toLowerCase()));
  const pages = Math.max(1, Math.ceil(rows.length / PER_PAGE));
  const shown = rows.slice(page * PER_PAGE, page * PER_PAGE + PER_PAGE);
  const columns: LedgerColumn<FundRowOut & { id: string; [key: string]: unknown }>[] = [
    {
      key: "name", label: "Scheme", figure: false, width: "minmax(240px, 2.6fr)",
      render: (r) => (
        <Link href={`/holdings/${r.isin}`} style={{ textDecoration: "none", color: "inherit", display: "flex", gap: 10, alignItems: "center" }}>
          <span aria-hidden style={{ flex: "0 0 32px", height: 32, borderRadius: 8, display: "grid", placeItems: "center", font: "600 11px var(--font-label)", color: "var(--chart-1)", background: "color-mix(in srgb, var(--chart-1) 14%, transparent)" }}>
            {r.name.split(/\s+/).filter((w) => /^[A-Za-z]/.test(w)).slice(0, 2).map((w) => w[0]!.toUpperCase()).join("")}
          </span>
          <span style={{ display: "grid", gap: 3, minWidth: 0 }}>
            <span style={{ font: "500 13px var(--font-body)", color: "var(--text-primary)" }}>{r.name}</span>
            <span style={{ display: "flex", gap: 8 }}>
              <LedgerTag>{r.category ?? r.asset_class ?? "Fund"}</LedgerTag>
              <LedgerTag tone={r.plan === "Regular" ? undefined : "accent"}>{r.plan}</LedgerTag>
            </span>
          </span>
        </Link>
      ),
    },
    { key: "weight", label: "Weight", align: "right", figure: false, render: (r) => <Pill color="var(--positive)">{formatPercent(r.weight, 1)}</Pill> },
    { key: "nav", label: "NAV", align: "right", render: (r) => formatMoney(r.nav, true) },
    { key: "invested", label: "Invested", align: "right", render: (r) => formatMoney(r.invested) },
    { key: "value", label: "Current", align: "right", render: (r) => formatMoney(r.value) },
    {
      key: "unrealised", label: "Unrealised", align: "right", figure: false,
      render: (r) => <LedgerSigned value={formatMoney(r.unrealised)} sub={formatPercent(r.unrealised_pct)} />,
    },
    { key: "fund_return_1y", label: "Fund 1Y", align: "right", figure: false, render: (r) => <Signed value={r.fund_return_1y} why={r.fund_return_reason} /> },
    { key: "fund_return_3y", label: "Fund 3Y · a year", align: "right", figure: false, render: (r) => <Signed value={r.fund_return_3y} why={r.fund_return_reason} /> },
  ];
  const chips = <T extends string>(values: readonly T[], value: T, set: (v: T) => void) => (
    <span style={{ display: "inline-flex", gap: 6, flexWrap: "wrap" }}>
      {values.map((v) => <button key={v} type="button" className="fa-chip" aria-pressed={v === value} onClick={() => { set(v); setPage(0); }}>{v}</button>)}
    </span>
  );
  return (
    <Panel title="My funds" sub={`${plural(data.funds.length, "scheme")} · weighted by current value`}
      action={
        <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          <div style={{ width: 220 }}><Input icon="search" placeholder="Search funds, fund houses" value={q} onChange={(v) => { setQ(v); setPage(0); }} /></div>
          {chips(["All", "Direct", "Regular"] as const, plan, setPlan)}
          {assets.length > 2 && chips(assets, asset, setAsset)}
        </div>
      }>
      <Wide>
        <LedgerGrid gap={16} numericFloor={84} columns={columns} rows={shown.map((f) => ({ ...f, id: f.isin }))}
          emptyMessage={q || plan !== "All" || asset !== "All" ? "No fund matches these filters." : "No mutual funds held."} />
      </Wide>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, font: "500 12.5px var(--font-label)", color: "var(--text-tertiary)" }}>
        <span>Showing {rows.length ? page * PER_PAGE + 1 : 0}–{Math.min(rows.length, (page + 1) * PER_PAGE)} of {rows.length}</span>
        <span style={{ display: "flex", gap: 6 }}>
          <button type="button" className="fa-chip" disabled={page === 0} onClick={() => setPage(page - 1)}>Previous</button>
          <button type="button" className="fa-chip" disabled={page >= pages - 1} onClick={() => setPage(page + 1)}>Next</button>
        </span>
      </div>
      <Note>Fund 1Y and 3Y are each scheme&apos;s own NAV return, not your return. They are withheld for IDCW options, whose NAV falls at every payout.</Note>
    </Panel>
  );
}

function Lines({ lines }: { lines: ReactNode[] }) {
  return (
    <ul style={{ margin: 0, padding: 0, listStyle: "none", display: "grid", gap: 10, font: "500 14.5px/1.5 var(--font-body)", color: "var(--text-secondary)" }}>
      {lines.filter(Boolean).map((line, i) => (
        <li key={i} style={{ display: "flex", gap: 10, alignItems: "baseline" }}>
          <i style={{ flex: "0 0 7px", height: 7, borderRadius: "50%", background: PALETTE[i % PALETTE.length], transform: "translateY(-1px)" }} />
          <span>{line}</span>
        </li>
      ))}
    </ul>
  );
}

/* ------------------------------------------------------------- diversification --- */

function AssetDonut({ groups, size = 168 }: { groups: FundGroupOut[]; size?: number }) {
  const top = groups[0];
  return (
    <DonutChart
      size={size}
      thickness={20}
      slices={groups.map((g, i) => ({ label: g.label, value: (num(g.share) ?? 0) * 100, color: PALETTE[i % PALETTE.length], display: formatPercent(g.share, 1) }))}
      centerValue={top ? formatPercent(top.share, 1) : "—"}
      centerLabel={top?.label.toUpperCase()}
      legendRuled
    />
  );
}

function DiversificationBasic({ data, onAdvanced }: { data: FundAnalyticsOut; onAdvanced: () => void }) {
  const cls = data.asset_classes[0], house = data.fund_houses[0];
  return (
    <Panel title="How your portfolio is spread" sub="By each scheme's type, fund house and SEBI category">
      <div style={{ display: "flex", gap: 28, alignItems: "center", flexWrap: "wrap" }}>
        <AssetDonut groups={data.asset_classes} size={150} />
        <div style={{ flex: "1 1 280px" }}>
          <Lines lines={[
            cls && <><b>{formatPercent(cls.share, 0)}</b> of your money sits in {cls.label.toLowerCase()} funds.</>,
            house && <><b>{house.label}</b> manages {formatPercent(house.share, 1)} of it{(num(house.share) ?? 0) > 0.5 ? " — above the 50% check" : ""}.</>,
            <>Your funds span {plural(data.categories.length, "SEBI category", "SEBI categories")}.</>,
          ]} />
        </div>
      </div>
      <SwitchFoot question="Want the fund-house and category breakdown?" onAdvanced={onAdvanced} />
    </Panel>
  );
}

function DiversificationAdvanced({ data }: { data: FundAnalyticsOut }) {
  return (
    <>
      <Panel span={6} title="Asset class allocation" sub="By each scheme's own type">
        <AssetDonut groups={data.asset_classes} />
      </Panel>
      <Panel span={6} title="Fund house concentration" sub="Check: no fund house above 50% of fund value">
        <ShareBars groups={data.fund_houses} limit={0.5} />
      </Panel>
      <Panel span={6} title="SEBI categories" sub="Where each scheme sits in SEBI's classification">
        <ShareBars groups={data.categories} />
      </Panel>
      <Panel span={6} title="Look-through and overlap" sub="Sectors, company shares and fund overlap">
        <div style={{ flex: 1, display: "grid", placeItems: "center", textAlign: "center", gap: 8, padding: "18px 8px", borderRadius: "var(--radius-md)", background: "var(--surface-sunken)" }}>
          <span className="fa-pill" style={{ color: "var(--text-tertiary)", border: "1px solid var(--border)" }}>Not available yet</span>
          <Note>
            Seeing which companies and sectors your funds own, and how much two funds overlap, needs
            each fund house&apos;s monthly portfolio disclosure. Trackr+ does not receive it yet, so
            nothing is estimated here.
          </Note>
        </div>
      </Panel>
    </>
  );
}

/** One bar per group, longest first; ``limit`` draws the check's line on the same scale. */
function ShareBars({ groups, limit }: { groups: FundGroupOut[]; limit?: number }) {
  const { masked } = usePrivacy();
  if (!groups.length) return <Note>Nothing valued yet.</Note>;
  return (
    <div style={{ display: "grid", gap: 10 }}>
      {groups.map((g, i) => {
        const share = num(g.share) ?? 0;
        const over = limit !== undefined && share > limit;
        return (
          <div key={g.label} style={{ display: "grid", gap: 5, padding: "8px 10px", borderRadius: "var(--radius-md)", background: over ? "color-mix(in srgb, var(--warning) 9%, transparent)" : "transparent" }}>
            <div style={{ display: "flex", justifyContent: "space-between", gap: 12, font: "600 12.5px var(--font-label)" }}>
              <span style={{ color: over ? "var(--warning)" : "var(--text-primary)" }}>{g.label}</span>
              <span style={{ color: "var(--text-secondary)", whiteSpace: "nowrap" }}>
                {compactInr(g.value, masked)} · <b style={{ color: over ? "var(--warning)" : "var(--text-primary)" }}>{formatPercent(g.share, 1)}</b>
              </span>
            </div>
            <div style={{ position: "relative", height: 6, borderRadius: 3, background: "var(--ink-100)" }}>
              <div style={{ width: `${share * 100}%`, height: "100%", borderRadius: 3, background: over ? "var(--warning)" : PALETTE[i % PALETTE.length] }} />
              {limit !== undefined && (
                <div title={`Check limit ${Math.round(limit * 100)}%`}
                  style={{ position: "absolute", left: `${limit * 100}%`, top: -3, width: 2, height: 12, background: "var(--text-tertiary)" }} />
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

/* --------------------------------------------------------------- risk & return --- */

function WindowControl({ win, setWin }: { win: WindowKey; setWin: (w: WindowKey) => void }) {
  return <SegmentedControl size="sm" options={WINDOWS.map((w) => ({ value: w, label: w }))} value={win} onChange={setWin} />;
}

function windowNote(w: FundWindowOut): string {
  const parts = [`Current mix: the funds you hold today at today's weights, month by month, ${formatDate(w.start)} to ${formatDate(w.end)}.`];
  const covered = num(w.covered_weight);
  if (covered !== null && covered < 0.9999) {
    parts.push(`Covers ${formatPercent(w.covered_weight, 1)} of fund value; ${w.excluded.map((e) => e.name).join(", ")} ${w.excluded.length > 1 ? "have" : "has"} no full NAV history for this window.`);
  }
  if (w.risk_free_rate === null) parts.push("No risk-free rate is stored for this window, so Sharpe, Sortino and alpha are withheld.");
  else parts.push(`Risk-free ${formatPercent(w.risk_free_rate, 2)} a year.`);
  return parts.join(" ");
}

function RiskBasic({ window, onAdvanced }: { window: FundWindowOut | null; onAdvanced: () => void }) {
  const mix = window?.mix, idx = window?.benchmark;
  const calmer = mix && idx && (num(mix.volatility) ?? 0) < (num(idx.volatility) ?? 0);
  return (
    <Panel title="How much your funds swing" sub="Your current mix over the last 3 years, month by month">
      {mix ? (
        <Lines lines={[
          <>Your current mix moved <b>{formatPercent(mix.volatility, 1)}</b> a year, {calmer ? "a little less" : "more"} than the {window!.benchmark_name} ({formatPercent(idx?.volatility, 1)}).</>,
          <>Its worst fall from a high was <b style={{ color: "var(--negative)" }}>{formatPercent(mix.max_drawdown, 1)}</b>, against {formatPercent(idx?.max_drawdown, 1)} for the index.</>,
          <>Over the same months it returned <b style={{ color: toneOf(mix.annual_return) }}>{formatPercent(mix.annual_return, 1)}</b> a year.</>,
        ]} />
      ) : <Note>{window?.reason ?? "Not enough NAV history yet."}</Note>}
      <SwitchFoot question="Want the return-vs-risk chart and every ratio?" onAdvanced={onAdvanced} />
    </Panel>
  );
}

function RiskAdvanced({ window, win, setWin }: { window: FundWindowOut | null; win: WindowKey; setWin: (w: WindowKey) => void }) {
  if (!window) return null;
  const metrics: { label: string; key: keyof FundStatsOut; kind: "pct" | "ratio"; foot: string }[] = [
    { label: "Alpha", key: "alpha", kind: "pct", foot: "a year, vs the index" },
    { label: "Beta", key: "beta", kind: "ratio", foot: "1.00 moves with the index" },
    { label: "Sharpe", key: "sharpe", kind: "ratio", foot: "return per unit of risk" },
    { label: "Sortino", key: "sortino", kind: "ratio", foot: "return per unit of downside" },
    { label: "Max drawdown", key: "max_drawdown", kind: "pct", foot: "worst month-end fall" },
    { label: "Std dev (σ)", key: "volatility", kind: "pct", foot: `${win} annualised` },
  ];
  const show = (s: FundStatsOut | null, key: keyof FundStatsOut, kind: "pct" | "ratio") =>
    !s ? "—" : kind === "pct" ? formatPercent(s[key]) : ratio(s[key]);
  const fundCols: LedgerColumn<{ id: string; name: string; weight: string | null; stats: FundStatsOut; [key: string]: unknown }>[] = [
    { key: "name", label: "Fund", figure: false, width: "minmax(200px, 2.2fr)", render: (f) => <span style={{ font: "500 13px var(--font-body)" }}>{f.name}</span> },
    { key: "weight", label: "Weight", align: "right", render: (f) => formatPercent(f.weight, 1) },
    { key: "ret", label: "Return a year", align: "right", figure: false, render: (f) => <Signed value={f.stats.annual_return} /> },
    { key: "vol", label: "Volatility", align: "right", render: (f) => formatPercent(f.stats.volatility) },
    { key: "dd", label: "Worst fall", align: "right", render: (f) => formatPercent(f.stats.max_drawdown) },
    { key: "sharpe", label: "Sharpe", align: "right", render: (f) => ratio(f.stats.sharpe) },
    { key: "beta", label: "Beta", align: "right", render: (f) => ratio(f.stats.beta) },
  ];
  return (
    <>
      <Panel title={<>Return per unit of risk <Pill color="var(--text-secondary)">{win}</Pill></>}
        sub="Volatility a year on the across, return a year up the side"
        action={<WindowControl win={win} setWin={setWin} />}>
        {window.mix ? (
          <>
            <Keys keys={[["Current mix", MIX], [window.benchmark_name, IDX], ["Each fund", "var(--chart-3)"]]} />
            <Scatter window={window} />
          </>
        ) : <Note>{window.reason}</Note>}
        <Note>{window.reason ?? windowNote(window)}</Note>
      </Panel>
      {window.mix && metrics.map((m) => {
        const v = window.mix![m.key];
        const color = m.key === "max_drawdown" ? "var(--negative)" : m.kind === "pct" && m.key === "alpha" ? toneOf(v) : "var(--text-primary)";
        return (
          <Panel key={m.key} span={2}>
            <span style={{ font: "600 10.5px var(--font-label)", letterSpacing: ".12em", textTransform: "uppercase", color: "var(--text-tertiary)" }}>{m.label}</span>
            <span style={{ font: "500 26px/1 var(--font-figure)", color: v === null ? "var(--text-tertiary)" : color }}>{show(window.mix, m.key, m.kind)}</span>
            <Note>{m.foot}<br />Index {show(window.benchmark, m.key, m.kind) === "—" && m.key === "beta" ? "1.00" : show(window.benchmark, m.key, m.kind)}</Note>
          </Panel>
        );
      })}
      {window.mix && (
        <Panel title="Each fund over the same months" sub="Monthly returns from published NAVs, annualised — the funds themselves, not when you invested">
          <Wide>
            <LedgerGrid gap={14} numericFloor={84} columns={fundCols} rows={window.funds.map((f) => ({ ...f, id: f.isin }))} />
          </Wide>
        </Panel>
      )}
    </>
  );
}

function Keys({ keys }: { keys: [string, string][] }) {
  return (
    <div style={{ display: "flex", gap: 16, flexWrap: "wrap", font: "500 12px var(--font-label)", color: "var(--text-secondary)" }}>
      {keys.map(([label, color]) => (
        <span key={label} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
          <i style={{ width: 9, height: 9, borderRadius: "50%", background: color, display: "inline-block" }} />{label}
        </span>
      ))}
    </div>
  );
}

/** Return (y) against volatility (x): every fund, the current mix and the index. */
function Scatter({ window }: { window: FundWindowOut }) {
  const [ref, w] = useWidth();
  const hover = useTip();
  type Pt = { label: string; x: number; y: number; r: number; color: string; strong?: boolean };
  const pts: Pt[] = [];
  for (const f of window.funds) {
    const x = num(f.stats.volatility), y = num(f.stats.annual_return);
    if (x !== null && y !== null) pts.push({ label: f.name, x, y, r: 5 + 12 * Math.sqrt(num(f.weight) ?? 0), color: "var(--chart-3)" });
  }
  const bx = num(window.benchmark?.volatility ?? null), by = num(window.benchmark?.annual_return ?? null);
  if (bx !== null && by !== null) pts.push({ label: window.benchmark_name, x: bx, y: by, r: 7, color: IDX, strong: true });
  const mx = num(window.mix?.volatility ?? null), my = num(window.mix?.annual_return ?? null);
  if (mx !== null && my !== null) pts.push({ label: "Current mix", x: mx, y: my, r: 10, color: MIX, strong: true });
  const h = 340, m = { l: 48, r: 16, t: 12, b: 44 };
  if (!pts.length) return <div ref={ref} style={{ height: h }} />;
  const xMax = Math.max(...pts.map((p) => p.x)) * 1.15 || 0.1;
  const yLo = Math.min(0, ...pts.map((p) => p.y)) * 1.15, yHi = Math.max(0, ...pts.map((p) => p.y)) * 1.15 || 0.1;
  const iw = Math.max(1, w - m.l - m.r), ih = h - m.t - m.b;
  const X = (v: number) => m.l + (v / xMax) * iw, Y = (v: number) => m.t + ((yHi - v) / (yHi - yLo)) * ih;
  const ticks = (lo: number, hi: number) => Array.from({ length: 5 }, (_, i) => lo + ((hi - lo) * i) / 4);
  const pct = (v: number) => `${(v * 100).toFixed(0)}%`;
  return (
    <div ref={ref}>{w > 0 && (
      <svg width={w} height={h} role="img" aria-label="Return against volatility">
        {ticks(yLo, yHi).map((t) => (
          <g key={`y${t}`}>
            <line x1={m.l} x2={w - m.r} y1={Y(t)} y2={Y(t)} style={{ stroke: "var(--chart-grid)", strokeDasharray: "2 3" }} />
            <text x={m.l - 6} y={Y(t)} dy=".32em" textAnchor="end" style={AXIS}>{pct(t)}</text>
          </g>
        ))}
        {ticks(0, xMax).map((t) => <text key={`x${t}`} x={X(t)} y={h - 24} textAnchor="middle" style={AXIS}>{pct(t)}</text>)}
        <line x1={m.l} x2={w - m.r} y1={Y(0)} y2={Y(0)} style={{ stroke: "var(--ink-400)" }} />
        {by !== null && <line x1={m.l} x2={w - m.r} y1={Y(by)} y2={Y(by)} style={{ stroke: IDX, strokeDasharray: "6 4" }} />}
        <text x={w - m.r} y={h - 4} textAnchor="end" style={AXIS}>VOLATILITY A YEAR →</text>
        {pts.map((p) => (
          <g key={p.label} onMouseMove={(e) => hover.show(e, `${p.label}: ${(p.y * 100).toFixed(1)}% a year, volatility ${(p.x * 100).toFixed(1)}%`)} onMouseLeave={hover.hide}>
            <circle cx={X(p.x)} cy={Y(p.y)} r={p.r} style={{ fill: p.color, fillOpacity: p.strong ? 0.9 : 0.45, stroke: p.color }} />
            {(p.strong || (window.funds.length <= 6 && w >= 560)) && (() => {
              // Labels on the right half sit to the LEFT of their dot, so none runs off the chart.
              const left = X(p.x) > m.l + iw * 0.55;
              return (
                <text x={left ? X(p.x) - p.r - 4 : X(p.x) + p.r + 4} y={Y(p.y)} dy=".32em" textAnchor={left ? "end" : "start"}
                  style={{ ...INK2, fontWeight: p.strong ? 600 : 500 }}>
                  {p.label.length > 26 ? `${p.label.slice(0, 25)}…` : p.label}
                </text>
              );
            })()}
          </g>
        ))}
      </svg>)}</div>
  );
}

/* ----------------------------------------------------------------- performance --- */

const grew = (s: string | null | undefined) => {
  const v = num(s);
  return v === null ? null : (v / 100 - 1).toString();
};

function PerformanceBasic({ data, window, onAdvanced }: { data: FundAnalyticsOut; window: FundWindowOut | null; onAdvanced: () => void }) {
  const last = window?.levels.at(-1);
  const gains = data.contributions.filter((c) => !isNeg(c.gain));
  const best = data.contributions[0];
  const total = totalGain(data.contributions);
  return (
    <Panel title="How your funds have done" sub="Your own return, and the current mix against the index">
      <Lines lines={[
        <>You have earned <b style={{ color: toneOf(data.xirr) }}>{formatPercent(data.xirr)}</b> a year on your fund money (XIRR).</>,
        last && <>Over 3 years your current mix grew <b style={{ color: toneOf(grew(last.mix)) }}>{formatPercent(grew(last.mix), 1)}</b>; the {window!.benchmark_name} grew {formatPercent(grew(last.benchmark), 1)}.</>,
        best && <><b>{best.name}</b> made the most: <b style={{ color: toneOf(best.gain) }}>{formatMoney(best.gain)}</b> of your {formatMoney(total.toString())} total gain.</>,
        <>{plural(gains.length, "fund")} of {data.contributions.length} made money.</>,
      ]} />
      <SwitchFoot question="Want the fund-by-fund breakdown?" onAdvanced={onAdvanced} />
    </Panel>
  );
}

function PerformanceAdvanced({ data, window, win, setWin }: { data: FundAnalyticsOut; window: FundWindowOut | null; win: WindowKey; setWin: (w: WindowKey) => void }) {
  const last = window?.levels.at(-1);
  const total = totalGain(data.contributions);
  const returnCols: LedgerColumn<FundRowOut & { id: string; [key: string]: unknown }>[] = [
    { key: "name", label: "Fund", figure: false, width: "minmax(180px, 2fr)", render: (r) => <span style={{ font: "500 13px var(--font-body)" }}>{r.name}</span> },
    { key: "fund_return_1y", label: "1Y", align: "right", figure: false, render: (r) => <Signed value={r.fund_return_1y} why={r.fund_return_reason} /> },
    { key: "fund_return_3y", label: "3Y · a year", align: "right", figure: false, render: (r) => <Signed value={r.fund_return_3y} why={r.fund_return_reason} /> },
  ];
  return (
    <>
      <Panel title={`Your funds vs ${window?.benchmark_name ?? "the index"}`}
        sub={last ? <>Current mix <b style={{ color: toneOf(grew(last.mix)) }}>{formatPercent(grew(last.mix), 1)}</b> vs {window!.benchmark_name} <b style={{ color: toneOf(grew(last.benchmark)) }}>{formatPercent(grew(last.benchmark), 1)}</b> · growth of 100</> : "Growth of 100"}
        action={<WindowControl win={win} setWin={setWin} />}>
        {!window || window.levels.length < 2 ? (
          <Note>{window?.reason ?? "Not enough NAV history for this window."}</Note>
        ) : (
          <VintageLinePlot
            labels={window.levels.map((l) => l.date)}
            series={[
              { label: "Current mix", points: window.levels.map((l) => l.mix), color: MIX, area: true },
              { label: window.benchmark_name, points: window.levels.map((l) => l.benchmark), color: IDX },
            ]}
            formatTick={(v) => v}
            formatLabel={(v) => formatDate(v)}
            maxXLabels={6}
            height={280}
          />
        )}
        <Note>Month-end values. The current mix holds today&apos;s funds at today&apos;s weights throughout, so this shows how the mix behaved, not what you earned; your own return is the XIRR.</Note>
      </Panel>
      <Panel span={7} title="Contribution to gain" sub="Which funds produced your gain, since your first purchase">
        <GainBars rows={data.contributions} />
        <div style={{ display: "flex", justifyContent: "space-between", gap: 12, borderTop: "1px solid var(--border)", paddingTop: 10, font: "500 12.5px var(--font-label)", color: "var(--text-tertiary)" }}>
          <span>{plural(data.contributions.length, "fund")}, sold funds included</span>
          <b style={{ color: toneOf(total.toString()) }}>Total {formatMoney(total.toString())}</b>
        </div>
      </Panel>
      <Panel span={5} title="Each fund's own return" sub="The scheme's NAV change — not your return">
        <Wide>
          <LedgerGrid gap={12} columns={returnCols} rows={data.funds.map((f) => ({ ...f, id: f.isin }))} />
        </Wide>
        <Note>Comparison with each fund&apos;s category needs peer data Trackr+ does not have yet.</Note>
      </Panel>
    </>
  );
}

function GainBars({ rows }: { rows: FundContributionOut[] }) {
  const [ref, w] = useWidth();
  const { masked } = usePrivacy();
  const hover = useTip();
  const scale = useMemo(() => decimalScale([...rows.map((r) => r.gain), "0"]), [rows]);
  if (!rows.length) return <Note>No fund transactions yet.</Note>;
  const bh = 30, h = rows.length * bh + 4, m = { l: Math.min(220, Math.max(110, w * 0.34)), r: 60 };
  const iw = Math.max(1, w - m.l - m.r);
  const X = (v: string) => m.l + (scale.position(v) ?? 0) * iw;
  return (
    <div ref={ref}>{w > 0 && (
      <svg width={w} height={h} role="img" aria-label="Gain by fund">
        {rows.map((r, i) => {
          const y = i * bh + 4, neg = isNeg(r.gain), x0 = X("0"), x1 = X(r.gain);
          const label = r.name.length > 30 ? `${r.name.slice(0, 29)}…` : r.name;
          return (
            <g key={r.isin} onMouseMove={(e) => hover.show(e, `${r.name}${r.held ? "" : " (sold)"}: ${formatMoney(r.gain)} · ${formatPercent(r.share_of_total, 1)} of the total`)} onMouseLeave={hover.hide}>
              <text x={m.l - 8} y={y + 10} dy=".32em" textAnchor="end" style={{ ...INK2, opacity: r.held ? 1 : 0.6 }}>{label}</text>
              <rect x={Math.min(x0, x1)} y={y} width={Math.max(1, Math.abs(x1 - x0))} height={bh - 10} rx={4}
                style={{ fill: neg ? "var(--negative)" : "var(--positive)", opacity: r.held ? 1 : 0.55 }} />
              <text x={neg ? x1 - 4 : x1 + 4} y={y + 10} dy=".32em" textAnchor={neg ? "end" : "start"} style={AXIS}>{compactInr(r.gain, masked)}</text>
            </g>
          );
        })}
        <line x1={X("0")} x2={X("0")} y1={0} y2={h} style={{ stroke: "var(--ink-400)" }} />
      </svg>)}</div>
  );
}

/* ----------------------------------------------------------------- diagnostics --- */

const STATUS: Record<FundCheckStatus, { label: string; color: string; tag: string }> = {
  warn: { label: "Worth a look", color: "var(--warning)", tag: "Warn" },
  pass: { label: "Passing", color: "var(--positive)", tag: "Pass" },
  info: { label: "For information", color: "var(--chart-1)", tag: "Info" },
  not_assessable: { label: "Not assessable", color: "var(--ink-400)", tag: "No data" },
};
const ORDER: FundCheckStatus[] = ["warn", "info", "not_assessable", "pass"];

function DiagnosticsSummary({ data }: { data: FundAnalyticsOut }) {
  const count = (s: FundCheckStatus) => data.checks.filter((c) => c.status === s).length;
  return (
    <Panel title={<>Portfolio diagnostics <Pill color="var(--positive)">{plural(data.checks.length, "check")}</Pill></>}
      sub="Rule-based checks across fund houses, plans, categories, SIPs and lock-ins">
      <div className="fa-grid" style={{ gap: 12 }}>
        {(["pass", "warn", "info", "not_assessable"] as FundCheckStatus[]).map((s) => (
          <div key={s} style={{ gridColumn: "span 3", border: "1px solid var(--border)", borderRadius: "var(--radius-md)", padding: "12px 14px", display: "grid", gap: 6 }}>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 7, font: "600 12.5px var(--font-label)", color: "var(--text-primary)" }}>
              <i style={{ width: 8, height: 8, borderRadius: "50%", background: STATUS[s].color }} />{STATUS[s].label}
            </span>
            <span style={{ font: "500 13px var(--font-label)", color: "var(--text-tertiary)" }}>
              <b style={{ font: "600 26px var(--font-figure)", color: STATUS[s].color, marginRight: 6 }}>{count(s)}</b>of {data.checks.length}
            </span>
          </div>
        ))}
      </div>
    </Panel>
  );
}

function DiagnosticsBasic({ data, onAdvanced }: { data: FundAnalyticsOut; onAdvanced: () => void }) {
  const warn = data.checks.filter((c) => c.status === "warn");
  return (
    <>
      <DiagnosticsSummary data={data} />
      <Panel title="What to look at" sub="Only the checks that flagged something">
        {warn.length ? <Lines lines={warn.map((c) => <><b>{c.title}:</b> {c.summary}.</>)} /> : <Note>Nothing needs attention right now.</Note>}
        <SwitchFoot question="Want every check with its details?" onAdvanced={onAdvanced} />
      </Panel>
    </>
  );
}

function DiagnosticsAdvanced({ data }: { data: FundAnalyticsOut }) {
  const [open, setOpen] = useState<Set<string>>(new Set());
  const checks = [...data.checks].sort((a, b) => ORDER.indexOf(a.status) - ORDER.indexOf(b.status));
  const all = open.size === checks.length;
  return (
    <>
      <DiagnosticsSummary data={data} />
      {checks.map((c) => (
        <CheckCard key={c.key} check={c} open={open.has(c.key)}
          toggle={() => setOpen((prev) => { const next = new Set(prev); next.has(c.key) ? next.delete(c.key) : next.add(c.key); return next; })} />
      ))}
      <div style={{ gridColumn: "1 / -1", display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <button type="button" className="fa-link" onClick={() => setOpen(all ? new Set() : new Set(checks.map((c) => c.key)))}>
          {all ? "Collapse all cards" : "Expand all cards"}
        </button>
        <Note>
          Trackr+ rules ({data.rule_version}); they flag things to look at, not a verdict. Not checked
          yet, for want of a source: overlap between funds, expense ratios, category performance, exit loads.
        </Note>
      </div>
    </>
  );
}

function CheckCard({ check, open, toggle }: { check: FundCheckOut; open: boolean; toggle: () => void }) {
  const s = STATUS[check.status];
  const hasDetail = check.items.length > 0;
  return (
    <Panel span={4} tint={check.status === "pass" || check.status === "warn" ? s.color : undefined}
      title={check.title}
      action={
        <span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
          {hasDetail && (
            <button type="button" className="fa-chip" onClick={toggle} aria-expanded={open}>
              {open ? "Hide details ▴" : "View details ▾"}
            </button>
          )}
          <Pill color={s.color}>{s.tag}</Pill>
        </span>
      }>
      <span style={{ font: "500 13.5px/1.5 var(--font-body)", color: "var(--text-primary)" }}>{check.summary}.</span>
      {open && (
        <div style={{ display: "grid", gap: 8 }}>
          {hasDetail && (
            <ul style={{ margin: 0, paddingLeft: 16, font: "500 12.5px/1.55 var(--font-label)", color: "var(--text-secondary)" }}>
              {check.items.map((item) => <li key={item}>{item}</li>)}
            </ul>
          )}
        </div>
      )}
      <Note>{check.rule}</Note>
    </Panel>
  );
}
