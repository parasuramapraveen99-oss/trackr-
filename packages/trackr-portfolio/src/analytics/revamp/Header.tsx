"use client";

/**
 * Greeting line + KPI strip above the tabs (review §3). Sits above the Basic/Advanced
 * switch and stays in place across tabs.
 *
 * The greeting is a code-written template of two SEPARATE facts (review §3): the Nifty 50's
 * own move for "markets", the portfolio's own move for "you". The reference attributed the
 * portfolio's move to "markets"; keeping the two apart is the fix.
 */

import type { AnalyticsResponse } from "@trackr/core";
import { formatDate } from "@trackr/core";
import { slipFootLabel, slipFootRow, slipFootValue, slipLabel, slipNum, slipRule, slipShell } from "@trackr/ui";
import { Info, Rs, fmtFrac, fmtPct, muted, reasonText } from "./common";

function timeOfDay(): string {
  const hour = Number(new Intl.DateTimeFormat("en-IN", { hour: "numeric", hour12: false, timeZone: "Asia/Kolkata" }).format(new Date()));
  return hour < 12 ? "Good morning" : hour < 17 ? "Good afternoon" : "Good evening";
}

export function Greeting({ data }: { data: AnalyticsResponse }) {
  const h = data.revamp?.header;
  if (!h) return null;
  const bench = h.bench_day?.value_pct;
  const day = h.day_change_pct;
  return <div style={{ font: "500 14px var(--font-body)", color: "var(--text-secondary)" }}>
    {timeOfDay()}.{" "}
    {bench == null
      ? <>The Nifty 50&apos;s last move is not available ({reasonText(h.bench_day?.reason)}).</>
      : <>The Nifty 50 {bench >= 0 ? "rose" : "fell"} {fmtPct(Math.abs(bench), 2)} on {h.bench_day?.date}.</>}{" "}
    {day == null
      ? <>Your portfolio&apos;s day change is not available.</>
      : <>Your portfolio {day >= 0 ? "rose" : "fell"} {fmtPct(Math.abs(day), 2)} (<Rs v={h.day_change} sign />).</>}{" "}
    {h.overall_gain != null && <>Overall, including realised gains and dividends, you are <Rs v={h.overall_gain} sign />.</>}
  </div>;
}

/** The dashboard's KPI slip (@trackr/ui VintageKpi), plus the ⓘ the analytics figures carry. */
function Card({ label, info, children, foot, footValue, tone }: {
  label: string; info?: React.ReactNode; children: React.ReactNode; foot?: React.ReactNode; footValue?: React.ReactNode; tone?: Tone;
}) {
  return <div className="rv-kpi" style={{ ...slipShell, display: "flex", flexDirection: "column" }}>
    <div style={{ ...slipLabel, display: "flex", alignItems: "center", gap: 2 }}>{label}{info}</div>
    <div style={{ ...slipNum(tone === "neg"), fontSize: "clamp(22px, 1.9vw, 32px)", color: tone === "pos" ? "var(--positive)" : slipNum(tone === "neg").color }}>{children}</div>
    <div style={{ ...slipRule, marginTop: "auto", paddingTop: 10 }} />
    <div style={slipFootRow}>
      <span style={slipFootLabel}>{foot}</span>
      {footValue != null && <span style={slipFootValue(tone === "neg")}>{footValue}</span>}
    </div>
  </div>;
}

type Tone = "pos" | "neg" | undefined;
const signTone = (v: string | number | null | undefined): Tone =>
  v == null ? undefined : String(v).startsWith("-") ? "neg" : "pos";

export function KpiStrip({ data, onHealth }: { data: AnalyticsResponse; onHealth?: () => void }) {
  const h = data.revamp?.header;
  if (!h) return null;
  const grade = h.health.grade, score = h.health.score, bounds = h.health.bounds;
  return <div className="rv-kpis">
    <Card label="Invested (current holdings)" info={<Info title="Invested (current holdings)">
      Weighted-average cost of the shares you hold today, from your transaction ledger. It is not every rupee
      you have ever put in: sold positions are in Overall gain.
    </Info>} foot="Principal" footValue={`${data.positions.length} holdings`}><Rs v={h.invested} /></Card>
    <Card label="Worth today" info={<Info title="Worth today">
      Holdings at the latest official exchange close. A holding without a price is named below and left out, never valued at zero.
    </Info>} foot="Marked to market" footValue={formatDate(data.as_of)}><Rs v={h.worth} /></Card>
    <Card label="Unrealised P&L" tone={signTone(h.unrealised)} foot="On invested" footValue={fmtPct(h.unrealised_pct, 2, true)} info={<Info title="Unrealised P&L">
      Worth today minus the cost of what you still hold. Gains already booked and dividends are not here.
    </Info>}><Rs v={h.unrealised} sign /></Card>
    <Card label="Overall gain" tone={signTone(h.overall_gain)}
      foot={h.xirr_all != null ? <>XIRR a year since {h.xirr_all_start}
        {(h.xirr_all_coverage_pct ?? 100) < 95 && <span style={{ color: "var(--warning)" }}> · covers {fmtPct(h.xirr_all_coverage_pct, 0)} of today&apos;s value</span>}</> : undefined}
      footValue={h.xirr_all != null ? fmtFrac(h.xirr_all, 1, true) : undefined}
      info={<Info title="Overall gain" calc={<>
        Unrealised <Rs v={h.unrealised} sign /> + realised (FIFO) <Rs v={h.realised} sign /> + dividends received <Rs v={h.dividends_received} sign />.
        Dividends count only when recorded in your ledger.
      </>}>Everything this portfolio has made or lost: open positions, sold positions and dividends received.</Info>}>
      {h.overall_gain == null ? <span style={muted}>Not available: {h.overall_gain_missing.join(", ")} missing</span> : <Rs v={h.overall_gain} sign />}
    </Card>
    <Card label="Today's change" tone={signTone(h.day_change)}
      foot={h.day_check?.status === "mismatch"
        ? <span style={{ color: "var(--negative)" }}>Does not match the snapshot difference (gap <Rs v={h.day_check.gap} />). Check prices.</span>
        : <>Of previous <Rs v={h.previous_value} /></>}
      footValue={h.day_check?.status === "mismatch" ? undefined : fmtPct(h.day_change_pct, 2, true)}
      info={<Info title="Today's change" calc={h.day_check?.status === "matches" ? "Matches the stored snapshot difference." : reasonText(h.day_check?.reason)}>
        Σ shares × (today&apos;s close − previous close), as a share of the previous session&apos;s value.
      </Info>}>
      {h.day_check?.status === "mismatch" ? <span style={{ color: "var(--negative)" }}>Mismatch</span> : <Rs v={h.day_change} sign />}
    </Card>
    <Card label="Portfolio health" info={<Info title="Portfolio health">
      {data.health.rule_version}: concentration, diversification, observed risk, business quality and valuation. When a part
      cannot be scored the grade is withheld and the possible range is shown.
    </Info>} foot={onHealth && <button type="button" onClick={onHealth} style={{ border: 0, background: "none", padding: 0, cursor: "pointer", font: "inherit", letterSpacing: "inherit", textTransform: "inherit", color: "var(--text-link)" }}>See the parts →</button>}
      footValue={grade != null ? `${score}/100` : undefined}>
      {grade != null ? grade
        : <span style={{ color: "var(--text-secondary)" }}>Withheld{bounds && <span style={{ fontSize: "0.5em" }}> · {bounds.lower.toFixed(0)}–{bounds.upper.toFixed(0)}</span>}</span>}
    </Card>
  </div>;
}
